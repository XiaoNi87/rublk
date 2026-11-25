// SPDX-License-Identifier: GPL-2.0-or-later
//! Fixed LE records and preserve-one-copy transitions. No writable admission here.
use super::geometry::{Geometry, CHUNK, PREFIX};
use anyhow::{bail, ensure, Context, Result};
pub const RECORD_BYTES: usize = 4096;
pub const SLOT_OFFSETS: [u64; 2] = [0, 4096];
pub const SEAL_OFFSET: u64 = 8192;
const MAGIC: &[u8; 8] = b"RUBLK5\0\0";
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum RecordState {
    Clean = 1,
    Dirty = 2,
    Seal = 3,
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Identity {
    pub array: [u8; 16],
    pub members: [[u8; 16]; 3],
    pub member: u32,
    pub member_bytes: u64,
    pub sector: u32,
}
impl Identity {
    fn validate(&self) -> Result<()> {
        ensure!(self.member < 3, "invalid member index");
        ensure!(self.array != [0; 16], "zero array identity");
        ensure!(
            self.sector == 512 || self.sector == 4096,
            "unsupported LBA size"
        );
        ensure!(
            self.member_bytes % u64::from(self.sector) == 0,
            "unaligned member"
        );
        for i in 0..3 {
            ensure!(self.members[i] != [0; 16], "zero member identity");
            for j in 0..i {
                ensure!(
                    self.members[i] != self.members[j],
                    "duplicate member identity"
                );
            }
        }
        Geometry::raid5([self.member_bytes; 3])?;
        Ok(())
    }
    fn compatible(&self, other: &Self) -> bool {
        self.array == other.array
            && self.members == other.members
            && self.member_bytes == other.member_bytes
            && self.sector == other.sector
    }
}
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct Record {
    pub identity: Identity,
    pub generation: u64,
    pub state: RecordState,
}
pub fn crc32c(bytes: &[u8]) -> u32 {
    let mut crc = !0u32;
    for &b in bytes {
        crc ^= u32::from(b);
        for _ in 0..8 {
            crc = (crc >> 1) ^ (0x82f63b78u32.wrapping_mul(crc & 1));
        }
    }
    !crc
}
fn put32(b: &mut [u8], at: usize, n: u32) {
    b[at..at + 4].copy_from_slice(&n.to_le_bytes());
}
fn put64(b: &mut [u8], at: usize, n: u64) {
    b[at..at + 8].copy_from_slice(&n.to_le_bytes());
}
fn get32(b: &[u8], at: usize) -> u32 {
    u32::from_le_bytes(b[at..at + 4].try_into().unwrap())
}
fn get64(b: &[u8], at: usize) -> u64 {
    u64::from_le_bytes(b[at..at + 8].try_into().unwrap())
}
impl Record {
    pub fn encode(&self) -> Result<[u8; RECORD_BYTES]> {
        self.identity.validate()?;
        ensure!(
            (self.state == RecordState::Seal) == (self.generation == 0),
            "invalid generation/state"
        );
        let mut b = [0; RECORD_BYTES];
        b[..8].copy_from_slice(MAGIC);
        put32(&mut b, 8, 1);
        put32(&mut b, 12, RECORD_BYTES as u32);
        put32(&mut b, 20, self.state as u32);
        b[24..40].copy_from_slice(&self.identity.array);
        for i in 0..3 {
            b[40 + i * 16..56 + i * 16].copy_from_slice(&self.identity.members[i]);
        }
        put32(&mut b, 88, self.identity.member);
        put32(&mut b, 92, self.identity.sector);
        put64(&mut b, 96, self.identity.member_bytes);
        put64(&mut b, 104, CHUNK);
        put64(&mut b, 112, PREFIX);
        put64(
            &mut b,
            120,
            Geometry::raid5([self.identity.member_bytes; 3])?.capacity(),
        );
        put64(&mut b, 128, self.generation);
        b[136..152].copy_from_slice(&self.identity.members[self.identity.member as usize]);
        let crc = crc32c(&b);
        put32(&mut b, 16, crc);
        Ok(b)
    }
    pub fn decode(raw: &[u8; RECORD_BYTES]) -> Result<Self> {
        ensure!(
            &raw[..8] == MAGIC && get32(raw, 8) == 1 && get32(raw, 12) == 4096,
            "unknown record format"
        );
        let mut b = *raw;
        let expected = get32(&b, 16);
        put32(&mut b, 16, 0);
        ensure!(crc32c(&b) == expected, "record CRC32C mismatch");
        ensure!(b[152..].iter().all(|n| *n == 0), "nonzero reserved bytes");
        let state = match get32(&b, 20) {
            1 => RecordState::Clean,
            2 => RecordState::Dirty,
            3 => RecordState::Seal,
            _ => bail!("unknown record state"),
        };
        let identity = Identity {
            array: b[24..40].try_into().unwrap(),
            members: std::array::from_fn(|i| b[40 + i * 16..56 + i * 16].try_into().unwrap()),
            member: get32(&b, 88),
            sector: get32(&b, 92),
            member_bytes: get64(&b, 96),
        };
        let record = Self {
            identity,
            generation: get64(&b, 128),
            state,
        };
        // Canonical re-encoding verifies geometry, explicit member UUID and reserved fields.
        ensure!(
            record.encode()? == *raw,
            "noncanonical metadata geometry or member UUID"
        );
        Ok(record)
    }
}
// A CRC-valid but unsupported/noncanonical record is not a torn empty slot.
fn slot_record(raw: &[u8; RECORD_BYTES]) -> Result<Option<Record>> {
    let mut bytes = *raw;
    let expected = get32(&bytes, 16);
    put32(&mut bytes, 16, 0);
    if crc32c(&bytes) != expected {
        return Ok(None);
    }
    Ok(Some(Record::decode(raw)?))
}
/// Implementations must fence *all old kernel and metadata I/O*, not just a PID/lock.
/// Short read/write completion is an error. Flush success is durable by backend contract.
pub trait MetadataIo {
    fn fence(&mut self) -> Result<()>;
    fn flush(&mut self, member: usize) -> Result<()>;
    fn read(&mut self, member: usize, offset: u64) -> Result<[u8; RECORD_BYTES]>;
    fn write(&mut self, member: usize, offset: u64, bytes: &[u8; RECORD_BYTES]) -> Result<()>;
    /// One independently durable update per member. This phase must fully join
    /// before a caller can submit any survivor overwrite in its second phase.
    fn persist_members(&mut self, updates: &[(usize, u64, [u8; RECORD_BYTES])]) -> Result<()> {
        let mut seen = [false; 3];
        for (member, _, _) in updates {
            ensure!(
                *member < 3 && !seen[*member],
                "duplicate/out-of-range metadata phase member"
            );
            seen[*member] = true;
        }
        for (member, offset, bytes) in updates {
            self.write(*member, *offset, bytes)?;
            self.flush(*member)?;
        }
        Ok(())
    }
}
pub struct Snapshot {
    pub records: [[Option<Record>; 2]; 3],
    pub identities: [Identity; 3],
    pub clean: bool,
    pub generation: u64,
}
pub fn inspect(io: &mut impl MetadataIo) -> Result<Snapshot> {
    io.fence()?;
    // No overwrite or survivor choice before every baseline flush succeeds.
    for m in 0..3 {
        io.flush(m)?;
    }
    let mut records: [[Option<Record>; 2]; 3] = std::array::from_fn(|_| [None, None]);
    let mut seals = Vec::new();
    for m in 0..3 {
        let seal = Record::decode(&io.read(m, SEAL_OFFSET)?)?;
        ensure!(
            seal.state == RecordState::Seal && seal.identity.member as usize == m,
            "incomplete/foreign creation seal"
        );
        for (slot, off) in SLOT_OFFSETS.into_iter().enumerate() {
            let raw = io.read(m, off)?;
            let record = slot_record(&raw)?;
            if let Some(r) = &record {
                ensure!(
                    r.state != RecordState::Seal && r.identity == seal.identity,
                    "incompatible valid slot identity"
                );
            }
            records[m][slot] = record;
        }
        ensure!(
            records[m].iter().any(Option::is_some),
            "member {m}: no valid identity survivor"
        );
        seals.push(seal);
    }
    for seal in &seals[1..] {
        ensure!(
            seal.identity.compatible(&seals[0].identity),
            "members from different arrays/geometry"
        );
    }
    let generation = records
        .iter()
        .flatten()
        .filter_map(|r| r.as_ref().map(|r| r.generation))
        .max()
        .unwrap();
    let clean = records.iter().flatten().all(|r| {
        r.as_ref()
            .is_some_and(|r| r.state == RecordState::Clean && r.generation == generation)
    });
    let identities = std::array::from_fn(|m| seals[m].identity.clone());
    Ok(Snapshot {
        records,
        identities,
        clean,
        generation,
    })
}
/// Caller keeps mutations frozen; success is the only permission to start writes.
pub fn mark_dirty(io: &mut impl MetadataIo) -> Result<u64> {
    transition(io, RecordState::Dirty)
}
pub(crate) fn mark_clean(
    io: &mut impl MetadataIo,
    _proof: &super::controller::Drained,
    shared: &super::engine::Shared,
) -> Result<u64> {
    ensure!(
        shared.stop.load(std::sync::atomic::Ordering::Acquire)
            && !shared.error.load(std::sync::atomic::Ordering::Acquire)
            && shared.sync_done(),
        "CLEAN forbidden: active/incomplete/failed session"
    );
    transition(io, RecordState::Clean)
}
fn transition(io: &mut impl MetadataIo, state: RecordState) -> Result<u64> {
    ensure!(state != RecordState::Seal, "seal is immutable");
    let snapshot = inspect(io)?;
    let generation = snapshot
        .generation
        .checked_add(1)
        .context("metadata generation overflow")?;
    let mut first = Vec::new();
    let mut second = Vec::new();
    for m in 0..3 {
        let slots = &snapshot.records[m];
        let target = match (&slots[0], &slots[1]) {
            (None, Some(_)) => 0,
            (Some(_), None) => 1,
            (Some(a), Some(b)) => usize::from(a.generation > b.generation),
            _ => unreachable!(),
        };
        let record = Record {
            identity: snapshot.identities[m].clone(),
            generation,
            state,
        }
        .encode()?;
        first.push((m, SLOT_OFFSETS[target], record));
        second.push((m, SLOT_OFFSETS[1 - target], record));
    }
    io.persist_members(&first)?;
    io.persist_members(&second)?;
    Ok(generation)
}
pub fn resume_identities(io: &mut impl MetadataIo, array: [u8; 16]) -> Result<[Identity; 3]> {
    io.fence()?;
    for m in 0..3 {
        io.flush(m)?;
    }
    let mut ids = Vec::new();
    for m in 0..3 {
        let mut identity = None;
        for off in SLOT_OFFSETS {
            if let Some(record) = slot_record(&io.read(m, off)?)? {
                ensure!(
                    record.state != RecordState::Seal
                        && record.identity.array == array
                        && record.identity.member as usize == m,
                    "foreign resume member"
                );
                if let Some(previous) = &identity {
                    ensure!(
                        previous == &record.identity,
                        "conflicting resume identities"
                    );
                }
                identity = Some(record.identity);
            }
        }
        ids.push(identity.context("resume member has no original identity record")?);
    }
    for id in &ids[1..] {
        ensure!(
            id.compatible(&ids[0]),
            "resume member order/geometry conflict"
        );
    }
    Ok(ids.try_into().unwrap())
}
/// Provision only after the supplied bounded-memory, read-only parity check
/// succeeds. No normal device is published here. Resume never creates new IDs.
pub fn provision<I: MetadataIo>(
    io: &mut I,
    identities: [Identity; 3],
    resume: bool,
    readonly_check: impl FnOnce(&mut I) -> Result<()>,
) -> Result<u64> {
    for (m, id) in identities.iter().enumerate() {
        id.validate()?;
        ensure!(
            id.member as usize == m && id.compatible(&identities[0]),
            "incompatible provisioning identities"
        );
    }
    io.fence()?;
    for m in 0..3 {
        io.flush(m)?;
    }
    let mut slots: [[Option<Record>; 2]; 3] = std::array::from_fn(|_| [None, None]);
    let mut seals: [Option<Record>; 3] = [None, None, None];
    let mut max_generation = 0;
    for m in 0..3 {
        for offset in (0..PREFIX).step_by(RECORD_BYTES) {
            let raw = io.read(m, offset)?;
            if !resume {
                ensure!(
                    raw.iter().all(|b| *b == 0),
                    "fresh creation requires an empty metadata prefix"
                );
                continue;
            }
            if offset > SEAL_OFFSET {
                ensure!(
                    raw.iter().all(|b| *b == 0),
                    "unknown metadata outside reserved records"
                );
                continue;
            }
            if let Some(record) = slot_record(&raw)? {
                ensure!(record.identity == identities[m], "resume identity mismatch");
                if offset == SEAL_OFFSET {
                    ensure!(
                        record.state == RecordState::Seal,
                        "invalid creation seal state"
                    );
                    seals[m] = Some(record);
                } else {
                    ensure!(record.state != RecordState::Seal, "seal in mutable slot");
                    max_generation = max_generation.max(record.generation);
                    slots[m][offset as usize / 4096] = Some(record);
                }
            }
        }
        if resume {
            ensure!(
                slots[m].iter().any(Option::is_some),
                "resume requires original identity survivor on every member"
            );
        }
    }
    // A complete set belongs to ordinary assembly, not partial-create repair.
    if resume {
        ensure!(
            !seals.iter().all(Option::is_some),
            "already sealed array; use assemble"
        );
    }
    readonly_check(io)?;
    let generation = max_generation
        .checked_add(1)
        .context("creation generation overflow")?;
    let mut first = Vec::new();
    let mut second = Vec::new();
    for m in 0..3 {
        let target = if resume {
            match (&slots[m][0], &slots[m][1]) {
                (None, Some(_)) => 0,
                (Some(_), None) => 1,
                (Some(a), Some(b)) => usize::from(a.generation > b.generation),
                _ => unreachable!(),
            }
        } else {
            0
        };
        let record = Record {
            identity: identities[m].clone(),
            generation,
            state: RecordState::Dirty,
        }
        .encode()?;
        first.push((m, SLOT_OFFSETS[target], record));
        second.push((m, SLOT_OFFSETS[1 - target], record));
    }
    io.persist_members(&first)?;
    io.persist_members(&second)?;
    // No seal is submitted before all six DIRTY records are durable.
    let mut seals_to_write = Vec::new();
    for m in 0..3 {
        if seals[m].is_some() {
            continue;
        }
        let seal = Record {
            identity: identities[m].clone(),
            generation: 0,
            state: RecordState::Seal,
        }
        .encode()?;
        seals_to_write.push((m, SEAL_OFFSET, seal));
    }
    io.persist_members(&seals_to_write)?;
    Ok(generation)
}
#[cfg(test)]
#[path = "../../tests/raid5/metadata.rs"]
mod tests;
