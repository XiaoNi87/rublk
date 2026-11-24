// SPDX-License-Identifier: GPL-2.0-or-later
//! Informational snapshot only. Never authority for identity or persistence.
use super::engine::Shared;
use anyhow::Result;
use std::{
    fs::OpenOptions, io::Write, os::unix::fs::OpenOptionsExt, path::PathBuf, sync::atomic::Ordering,
};
pub fn path(id: u32) -> PathBuf {
    PathBuf::from("/run/rublk-raid5").join(format!("raid5-{id}.status.json"))
}
pub struct Publisher {
    path: PathBuf,
    temporary: PathBuf,
    uuid: [u8; 16],
    generation: u64,
}
impl Publisher {
    pub fn new(id: u32, uuid: [u8; 16], generation: u64) -> Self {
        let path = path(id);
        let temporary = path.with_extension(format!("{}.{}.tmp", std::process::id(), generation));
        Self {
            path,
            temporary,
            uuid,
            generation,
        }
    }
    pub fn publish(
        &self,
        shared: &Shared,
        phase: Option<&str>,
        clean_generation: Option<u64>,
    ) -> Result<()> {
        let state = phase.unwrap_or_else(|| {
            if shared.error.load(Ordering::Acquire) {
                "ERROR"
            } else if shared.stop.load(Ordering::Acquire) {
                "STOPPING"
            } else if shared.sync_done() {
                "RUNNING"
            } else {
                "RECOVERING"
            }
        });
        let started = shared.sync_started_ns.load(Ordering::Acquire);
        let completed = shared.sync_completed_ns.load(Ordering::Acquire);
        let scanned = shared.sync_scan_completed_ns.load(Ordering::Acquire);
        let finalizing = shared.sync_finalization_started_ns.load(Ordering::Acquire);
        let control = shared.sync_control.snapshot();
        let value = serde_json::json!({"version":1,"uuid":super::session::uuid_string(self.uuid),"generation":self.generation,"pid":std::process::id(),"state":state,"sample_unix_ns":super::engine::now_ns(),"sync":{"frontier":shared.first_unsynced(),"total_stripes":shared.geometry.stripes(),"started_unix_ns":started,"completed_unix_ns":completed,"scan_completed_unix_ns":scanned,"finalization_started_unix_ns":finalizing,"finalizing":finalizing!=0 && completed==0 && !shared.stop.load(Ordering::Acquire),"active":started!=0 && scanned==0 && !control.paused && !shared.stop.load(Ordering::Acquire),"pause_requested":control.requested,"paused":control.paused,"inflight_transactions":control.active,"rate_bytes_per_sec":control.rate},"failed_transactions":shared.failed_transactions.load(Ordering::Acquire),"clean_generation":clean_generation});
        let parent = self.path.parent().unwrap();
        std::fs::create_dir_all(parent)?;
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700))?;
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&self.temporary)?;
        // tmpfs status has no durability role. Atomic rename gives readers one sample.
        let result = (|| -> Result<()> {
            file.write_all(&serde_json::to_vec(&value)?)?;
            drop(file);
            std::fs::rename(&self.temporary, &self.path)?;
            Ok(())
        })();
        if result.is_err() {
            let _ = std::fs::remove_file(&self.temporary);
        }
        result
    }
}
