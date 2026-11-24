// SPDX-License-Identifier: GPL-2.0-or-later
//! One metadata-backed writable session. JSON is a hint; member records rule.
use super::{
    backend::Backends,
    buffer::BufferBudget,
    controller::Drained,
    engine::Shared,
    metadata::{self, Identity},
    startup,
};
use anyhow::{ensure, Context, Result};
use rand::RngCore;
use serde::{Deserialize, Serialize};
use std::{path::PathBuf, sync::Arc};
#[derive(Clone, Debug, Serialize, Deserialize)]
pub enum StartMode {
    Create,
    Assemble,
    Resume { array: [u8; 16] },
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct Config {
    pub paths: [PathBuf; 3],
    pub test_files: bool,
    pub mode: StartMode,
    pub sync_window: usize,
    pub sync_rate: u64,
    pub shards: usize,
    pub memory_limit: usize,
    pub expected_uuid: Option<[u8; 16]>,
    pub logical_sector: Option<u32>,
}
pub struct Session {
    pub backends: Arc<Backends>,
    pub shared: Arc<Shared>,
    pub config: Config,
    pub generation: u64,
    pub identities: [Identity; 3],
    pub budget: BufferBudget,
}
pub enum StopOutcome {
    Clean { generation: u64 },
    Dirty,
}
fn new_uuid() -> Result<[u8; 16]> {
    let mut id = [0; 16];
    rand::rngs::OsRng
        .try_fill_bytes(&mut id)
        .map_err(|e| anyhow::anyhow!("UUID entropy: {e}"))?;
    id[6] = (id[6] & 15) | 64;
    id[8] = (id[8] & 63) | 128;
    Ok(id)
}
fn validate(backends: &Backends, ids: &[Identity; 3], expected: Option<[u8; 16]>) -> Result<()> {
    for (m, id) in ids.iter().enumerate() {
        ensure!(
            id.member_bytes == backends.member_bytes()[m]
                && id.sector == backends.sectors[m]
                && id.member as usize == m,
            "on-member geometry differs from actual backend"
        );
    }
    if let Some(array) = expected {
        ensure!(
            ids[0].array == array,
            "JSON/expected UUID differs from actual array"
        );
    }
    Ok(())
}
impl Session {
    pub fn prepare(
        mut config: Config,
        queues: usize,
        depth: usize,
        max_request: usize,
    ) -> Result<Self> {
        ensure!(
            queues > 0
                && depth > 0
                && depth <= 256
                && config.sync_window > 0
                && config.sync_window <= 256,
            "queue/depth/window outside supported range"
        );
        ensure!(
            config.shards.is_power_of_two() && config.shards <= 4096,
            "invalid shard count"
        );
        ensure!(
            max_request <= 16 * 1024 * 1024,
            "request exceeds supported 16 MiB bound"
        );
        let budget = BufferBudget::with_request_size(
            queues,
            depth,
            config.sync_window,
            config.memory_limit,
            max_request,
        )?;
        let mut backends = Backends::open(config.paths.clone(), config.test_files)?;
        if let Some(sector) = config.logical_sector {
            ensure!(
                sector == backends.sectors[0],
                "requested logical size differs from backend LBA"
            );
        }
        // Canonical absolute paths in persistent discovery JSON; identity validation
        // uses the already opened/exclusively owned file descriptions.
        for p in &mut config.paths {
            *p = p.canonicalize()?;
        }
        let (identities, generation, baseline) = match config.mode.clone() {
            StartMode::Create => {
                let array = new_uuid()?;
                let members = [new_uuid()?, new_uuid()?, new_uuid()?];
                let sizes = backends.member_bytes();
                let ids = std::array::from_fn(|m| Identity {
                    array,
                    members,
                    member: m as u32,
                    member_bytes: sizes[m],
                    sector: backends.sectors[m],
                });
                super::log(format_args!("RAID5 CREATE identity uuid={} member_uuids={:?}; retain for explicit create-resume",uuid_string(array),members.map(uuid_string)));
                let generation = metadata::provision(&mut backends, ids.clone(), false, |b| {
                    startup::verify_adoption(b)
                })?;
                (ids, generation, true)
            }
            StartMode::Assemble => {
                let snapshot = metadata::inspect(&mut backends)?;
                validate(&backends, &snapshot.identities, config.expected_uuid)?;
                let generation = metadata::mark_dirty(&mut backends)?;
                (snapshot.identities, generation, snapshot.clean)
            }
            StartMode::Resume { array } => {
                let ids = metadata::resume_identities(&mut backends, array)?;
                validate(&backends, &ids, Some(array))?;
                let generation = metadata::provision(&mut backends, ids.clone(), true, |b| {
                    startup::verify_adoption(b)
                })?;
                (ids, generation, false)
            }
        };
        config.expected_uuid = Some(identities[0].array);
        config.mode = StartMode::Assemble;
        let shared = Arc::new(Shared::new(
            backends.geometry,
            config.shards,
            config.sync_window,
            baseline,
        )?);
        backends.begin_io();
        Ok(Self {
            backends: Arc::new(backends),
            shared,
            config,
            generation,
            identities,
            budget,
        })
    }
    pub fn json(&self) -> serde_json::Value {
        serde_json::json!({"raid5":{"config":self.config,"generation":self.generation,"array_uuid":self.identities[0].array,"data_offset":super::geometry::PREFIX,"chunk_bytes":super::geometry::CHUNK,"capacity":self.shared.geometry.capacity()}})
    }
    pub fn finish(self, proof: Drained) -> Result<StopOutcome> {
        let mut backends = Arc::try_unwrap(self.backends)
            .map_err(|_| anyhow::anyhow!("backend owners survived joined worker teardown"))?;
        backends.complete_drain(&proof);
        if self.shared.error.load(std::sync::atomic::Ordering::Acquire) || !self.shared.sync_done()
        {
            return Ok(StopOutcome::Dirty);
        }
        let generation = metadata::mark_clean(&mut backends, &proof, &self.shared)
            .context("CLEAN transition failed; do not report clean shutdown")?;
        Ok(StopOutcome::Clean { generation })
    }
}
pub fn uuid_string(id: [u8; 16]) -> String {
    let text: String = id.iter().map(|b| format!("{b:02x}")).collect();
    format!(
        "{}-{}-{}-{}-{}",
        &text[..8],
        &text[8..12],
        &text[12..16],
        &text[16..20],
        &text[20..]
    )
}
pub fn parse_uuid(text: &str) -> Result<[u8; 16]> {
    let text = text.replace('-', "");
    ensure!(text.len() == 32 && text.is_ascii(), "invalid UUID");
    let mut id = [0; 16];
    for i in 0..16 {
        id[i] = u8::from_str_radix(&text[i * 2..i * 2 + 2], 16)?;
    }
    ensure!(id != [0; 16], "zero UUID");
    Ok(id)
}
