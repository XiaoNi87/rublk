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
mod tests {
    use super::*;
    #[derive(Clone)]
    struct Model {
        cache: [[[u8; 4096]; 3]; 3],
        disk: [[[u8; 4096]; 3]; 3],
        calls: usize,
        fail: Option<usize>,
        writes: usize,
        schedule: Vec<(usize, bool)>,
        scheduled_phase: usize,
        phases: usize,
        tear_bytes: usize,
        uncertain_flush: bool,
    }
    impl Model {
        fn established() -> Self {
            let mut data = [[[0; 4096]; 3]; 3];
            for m in 0..3 {
                let id = Identity {
                    array: [9; 16],
                    members: [[1; 16], [2; 16], [3; 16]],
                    member: m as u32,
                    member_bytes: PREFIX + 3 * CHUNK,
                    sector: 512,
                };
                for slot in 0..3 {
                    data[m][slot] = Record {
                        identity: id.clone(),
                        generation: if slot == 2 { 0 } else { 10 },
                        state: if slot == 2 {
                            RecordState::Seal
                        } else {
                            RecordState::Clean
                        },
                    }
                    .encode()
                    .unwrap();
                }
            }
            Self {
                cache: data,
                disk: data,
                calls: 0,
                fail: None,
                writes: 0,
                schedule: Vec::new(),
                scheduled_phase: 0,
                phases: 0,
                tear_bytes: 20,
                uncertain_flush: false,
            }
        }
        fn tick(&mut self) -> Result<()> {
            self.calls += 1;
            if self.fail == Some(self.calls) {
                bail!("injected operation failure")
            }
            Ok(())
        }
        fn crash(&mut self) {
            // A completed but unflushed 4 KiB write may tear the old on-media target.
            for m in 0..3 {
                for slot in 0..3 {
                    if self.cache[m][slot] != self.disk[m][slot] {
                        self.disk[m][slot][..self.tear_bytes]
                            .copy_from_slice(&self.cache[m][slot][..self.tear_bytes]);
                    }
                }
            }
            self.cache = self.disk;
        }
    }
    impl MetadataIo for Model {
        fn persist_members(&mut self, updates: &[(usize, u64, [u8; RECORD_BYTES])]) -> Result<()> {
            let phase = self.phases;
            self.phases += 1;
            if self.schedule.is_empty() || phase != self.scheduled_phase || updates.len() != 3 {
                for (member, offset, bytes) in updates {
                    self.write(*member, *offset, bytes)?;
                    self.flush(*member)?;
                }
            } else {
                for (member, flush) in self.schedule.clone() {
                    let (_, offset, bytes) = &updates[member];
                    if flush {
                        self.flush(member)?;
                    } else {
                        self.write(member, *offset, bytes)?;
                    }
                }
            }
            Ok(())
        }
        fn fence(&mut self) -> Result<()> {
            self.tick()
        }
        fn flush(&mut self, m: usize) -> Result<()> {
            let result = self.tick();
            if result.is_ok() || self.uncertain_flush {
                self.disk[m] = self.cache[m];
            }
            result
        }
        fn read(&mut self, m: usize, o: u64) -> Result<[u8; 4096]> {
            self.tick()?;
            Ok(if o as usize / 4096 < 3 {
                self.cache[m][o as usize / 4096]
            } else {
                [0; 4096]
            })
        }
        fn write(&mut self, m: usize, o: u64, b: &[u8; 4096]) -> Result<()> {
            self.writes += 1;
            let index = o as usize / 4096;
            // On failed write, model a prefix tear reaching persistent media immediately.
            if let Err(e) = self.tick() {
                self.cache[m][index][..self.tear_bytes].copy_from_slice(&b[..self.tear_bytes]);
                self.disk[m][index][..self.tear_bytes].copy_from_slice(&b[..self.tear_bytes]);
                return Err(e);
            }
            self.cache[m][index] = *b;
            Ok(())
        }
    }
    #[test]
    fn codec_and_crc() {
        assert_eq!(crc32c(b"123456789"), 0xe3069283);
        let m = Model::established();
        let bytes = m.disk[0][0];
        assert_eq!(Record::decode(&bytes).unwrap().encode().unwrap(), bytes);
        for pos in 0..4096 {
            let mut b = bytes;
            b[pos] ^= 1;
            assert!(Record::decode(&b).is_err());
        }
    }
    #[test]
    fn clean_requires_all_six_and_seals() {
        let mut m = Model::established();
        assert!(inspect(&mut m).unwrap().clean);
        m.cache[0][1][3] ^= 1;
        assert!(!inspect(&mut m).unwrap().clean);
        m.cache[0][2] = [0; 4096];
        assert!(inspect(&mut m).is_err());
    }
    #[test]
    fn every_transition_boundary_retains_identity() {
        for desired in [RecordState::Dirty, RecordState::Clean] {
            let mut base = Model::established();
            if desired == RecordState::Clean {
                mark_dirty(&mut base).unwrap();
            }
            let mut success = base.clone();
            success.calls = 0;
            transition(&mut success, desired).unwrap();
            let operations = success.calls;
            for fail in 1..=operations {
                let mut m = base.clone();
                m.calls = 0;
                m.fail = Some(fail);
                assert!(transition(&mut m, desired).is_err());
                m.crash();
                for member in 0..3 {
                    assert!(
                        m.disk[member][..2]
                            .iter()
                            .any(|b| Record::decode(b).is_ok()),
                        "lost identity fail={fail} member={member}"
                    );
                }
            }
        }
    }
    #[test]
    fn sole_survivor_transitions_and_resume_preserve_identity_at_every_failure() {
        for bad_slot in 0..2 {
            for resume in [false, true] {
                for desired in [RecordState::Dirty, RecordState::Clean] {
                    let mut base = Model::established();
                    let ids = std::array::from_fn(|m| {
                        Record::decode(&base.cache[m][0]).unwrap().identity
                    });
                    for member in 0..3 {
                        // Alternate the damaged slot across members; cover both choices.
                        base.cache[member][(member + bad_slot) % 2][17] ^= 1;
                        if resume {
                            base.cache[member][2] = [0; 4096];
                        }
                    }
                    base.disk = base.cache;
                    let run = |model: &mut Model| {
                        if resume {
                            provision(model, ids.clone(), true, |_| Ok(()))
                        } else {
                            transition(model, desired)
                        }
                    };
                    let mut success = base.clone();
                    run(&mut success).unwrap();
                    let operations = success.calls;
                    assert_eq!(
                        inspect(&mut success).unwrap().clean,
                        !resume && desired == RecordState::Clean
                    );
                    // Include baseline, read, first-slot and second-slot write/flush failures.
                    for fail in 1..=operations {
                        let mut model = base.clone();
                        model.fail = Some(fail);
                        assert!(run(&mut model).is_err());
                        model.crash();
                        for member in 0..3 {
                            let surviving: Vec<_> = model.disk[member][..2]
                                .iter()
                                .filter_map(|bytes| Record::decode(bytes).ok())
                                .collect();
                            assert!(!surviving.is_empty(), "lost sole survivor: bad={bad_slot} resume={resume} fail={fail} member={member}");
                            assert!(surviving.iter().all(|r| r.identity == ids[member]));
                        }
                    }
                }
            }
        }
    }
    #[test]
    fn restart_with_cached_newer_record_requires_baseline() {
        let mut m = Model::established();
        let mut r = Record::decode(&m.cache[0][1]).unwrap();
        r.generation = 11;
        r.state = RecordState::Dirty;
        m.cache[0][1] = r.encode().unwrap();
        // A10 is durable, B11 readable only. Failed baseline must cause zero overwrites.
        m.fail = Some(2);
        assert!(mark_dirty(&mut m).is_err());
        assert_eq!(m.writes, 0);
        m.crash();
        assert!(Record::decode(&m.disk[0][0]).is_ok());
        // Successful baseline stabilizes B11 before selecting older A10 as target.
        m.cache[0][1] = r.encode().unwrap();
        m.calls = 0;
        m.fail = Some(15);
        let _ = mark_dirty(&mut m);
        m.crash();
        assert_eq!(Record::decode(&m.disk[0][1]).unwrap().generation, 11);
    }
    #[test]
    fn cache_preserved_restart_all_members_states_and_resume_boundaries() {
        for member in 0..3 {
            for resume in [false, true] {
                for desired in [RecordState::Dirty, RecordState::Clean] {
                    let mut base = Model::established();
                    if desired == RecordState::Clean {
                        mark_dirty(&mut base).unwrap();
                    }
                    let ids = std::array::from_fn(|m| {
                        Record::decode(&base.cache[m][0]).unwrap().identity
                    });
                    let mut newer = Record::decode(&base.cache[member][1]).unwrap();
                    newer.generation += 1;
                    newer.state = desired;
                    base.cache[member][1] = newer.encode().unwrap();
                    if resume {
                        base.cache[member][2] = [0; 4096];
                        base.disk[member][2] = [0; 4096];
                    }
                    base.calls = 0;
                    base.writes = 0;
                    let run = |m: &mut Model| {
                        if resume {
                            provision(m, ids.clone(), true, |_| Ok(()))
                        } else {
                            transition(m, desired)
                        }
                    };
                    let mut success = base.clone();
                    run(&mut success).unwrap();
                    for boundary in 1..=success.calls {
                        let mut m = base.clone();
                        m.fail = Some(boundary);
                        assert!(run(&mut m).is_err());
                        if boundary <= 4 {
                            assert_eq!(m.writes, 0, "overwrote before baseline member={member} resume={resume} boundary={boundary}");
                        }
                        m.crash();
                        for disk in 0..3 {
                            let survivors: Vec<_> = m.disk[disk][..2]
                                .iter()
                                .filter_map(|b| Record::decode(b).ok())
                                .collect();
                            assert!(!survivors.is_empty(), "lost identity member={member} resume={resume} boundary={boundary} disk={disk}");
                            assert!(survivors.iter().all(|r| r.identity == ids[disk]));
                        }
                        if boundary > 4 {
                            assert!(m.disk[member][..2]
                                .iter()
                                .filter_map(|b| Record::decode(b).ok())
                                .any(|r| r.generation >= newer.generation));
                        }
                    }
                }
            }
        }
    }

    #[test]
    fn all_parallel_member_write_flush_interleavings_keep_a_survivor() {
        fn enumerate(
            state: [usize; 3],
            prefix: Vec<(usize, bool)>,
            out: &mut Vec<Vec<(usize, bool)>>,
        ) {
            if state == [2; 3] {
                out.push(prefix);
                return;
            }
            for member in 0..3 {
                if state[member] == 2 {
                    continue;
                }
                let mut next = state;
                next[member] += 1;
                let mut p = prefix.clone();
                p.push((member, state[member] == 1));
                enumerate(next, p, out);
            }
        }
        let mut schedules = Vec::new();
        enumerate([0; 3], Vec::new(), &mut schedules);
        assert_eq!(schedules.len(), 90);
        for desired in [RecordState::Dirty, RecordState::Clean] {
            for phase in 0..2 {
                for schedule in &schedules {
                    let mut base = Model::established();
                    if desired == RecordState::Clean {
                        mark_dirty(&mut base).unwrap();
                    }
                    base.calls = 0;
                    base.phases = 0;
                    base.schedule = schedule.clone();
                    base.scheduled_phase = phase;
                    let mut success = base.clone();
                    transition(&mut success, desired).unwrap();
                    // Baseline/read prefix is shared with other tests; enumerate
                    // every write/flush cut in the two barrier-separated stages.
                    for failure in success.calls - 11..=success.calls {
                        let mut m = base.clone();
                        m.fail = Some(failure);
                        assert!(transition(&mut m, desired).is_err());
                        m.crash();
                        for member in 0..3 {
                            assert!(m.disk[member][..2].iter().any(|b| Record::decode(b).is_ok()), "lost survivor phase={phase} member={member} cut={failure} schedule={schedule:?}");
                        }
                        if desired == RecordState::Dirty {
                            let all_new = m.disk.iter().all(|d| {
                                d[..2].iter().all(|b| {
                                    Record::decode(b).is_ok_and(|r| {
                                        r.state == RecordState::Dirty && r.generation == 11
                                    })
                                })
                            });
                            assert!(!all_new, "failed DIRTY conversion cannot admit data");
                        }
                    }
                }
            }
        }
    }

    #[test]
    fn torn_write_sizes_and_uncertain_flush_restart_do_not_lose_identity() {
        for desired in [RecordState::Dirty, RecordState::Clean] {
            for resume in [false, true] {
                for tear in [0, 1, 16, 20, 512, 4095, 4096] {
                    for uncertain in [false, true] {
                        let mut base = Model::established();
                        if desired == RecordState::Clean {
                            mark_dirty(&mut base).unwrap();
                        }
                        let ids = std::array::from_fn(|m| {
                            Record::decode(&base.cache[m][0]).unwrap().identity
                        });
                        for member in 0..3 {
                            let mut r = Record::decode(&base.cache[member][1]).unwrap();
                            r.generation += 1;
                            r.state = desired;
                            base.cache[member][1] = r.encode().unwrap();
                            if resume {
                                base.cache[member][2] = [0; 4096];
                                base.disk[member][2] = [0; 4096];
                            }
                        }
                        base.calls = 0;
                        base.writes = 0;
                        base.tear_bytes = tear;
                        base.uncertain_flush = uncertain;
                        let run = |m: &mut Model| {
                            if resume {
                                provision(m, ids.clone(), true, |_| Ok(()))
                            } else {
                                transition(m, desired)
                            }
                        };
                        let mut success = base.clone();
                        run(&mut success).unwrap();
                        for failure in 1..=success.calls {
                            let mut m = base.clone();
                            m.fail = Some(failure);
                            assert!(run(&mut m).is_err());
                            if failure <= 4 {
                                assert_eq!(m.writes, 0);
                            }
                            m.crash();
                            for member in 0..3 {
                                assert!(m.disk[member][..2].iter().filter_map(|b| Record::decode(b).ok()).any(|r| r.identity == ids[member]), "lost identity resume={resume} tear={tear} uncertain={uncertain} failure={failure} member={member}");
                            }
                        }
                    }
                }
            }
        }
    }

    #[test]
    fn generation_overflow_has_no_overwrite() {
        let mut m = Model::established();
        for member in 0..3 {
            for slot in 0..2 {
                let mut r = Record::decode(&m.cache[member][slot]).unwrap();
                r.generation = u64::MAX;
                m.cache[member][slot] = r.encode().unwrap();
            }
        }
        assert!(mark_dirty(&mut m).is_err());
        assert_eq!(m.writes, 0);
    }
    #[test]
    fn unsupported_but_crc_valid_slot_rejected() {
        let mut m = Model::established();
        put32(&mut m.cache[0][1], 8, 2);
        put32(&mut m.cache[0][1], 16, 0);
        let crc = crc32c(&m.cache[0][1]);
        put32(&mut m.cache[0][1], 16, crc);
        assert!(inspect(&mut m).is_err());
        assert_eq!(m.writes, 0);
    }
    #[test]
    fn fresh_create_checks_before_writes_and_seals() {
        let base = Model::established();
        let ids = std::array::from_fn(|m| Record::decode(&base.cache[m][0]).unwrap().identity);
        let mut empty = base.clone();
        empty.cache = [[[0; 4096]; 3]; 3];
        empty.disk = empty.cache;
        let mut rejected = empty.clone();
        assert!(provision(&mut rejected, ids.clone(), false, |_| bail!("bad parity")).is_err());
        assert_eq!(rejected.writes, 0);
        assert_eq!(
            provision(&mut empty, ids.clone(), false, |_| Ok(())).unwrap(),
            1
        );
        assert!(!inspect(&mut empty).unwrap().clean);
        assert!(provision(&mut empty, ids, false, |_| Ok(())).is_err());
    }
    #[test]
    fn resume_baseline_failure_never_repairs_seals() {
        let mut m = Model::established();
        let ids =
            std::array::from_fn(|member| Record::decode(&m.cache[member][0]).unwrap().identity);
        m.cache[1][2] = [0; 4096];
        m.fail = Some(2);
        assert!(provision(&mut m, ids, true, |_| Ok(())).is_err());
        assert_eq!(m.writes, 0);
    }
    #[test]
    fn resume_repairs_missing_seal_only_with_all_original_identities() {
        let mut m = Model::established();
        let ids =
            std::array::from_fn(|member| Record::decode(&m.cache[member][0]).unwrap().identity);
        m.cache[1][2] = [0; 4096];
        m.cache[0][1] = [0; 4096];
        provision(&mut m, ids.clone(), true, |_| Ok(())).unwrap();
        assert!(!inspect(&mut m).unwrap().clean);
        let mut bad = Model::established();
        bad.cache[1][2] = [0; 4096];
        bad.cache[0][0] = [0; 4096];
        bad.cache[0][1] = [0; 4096];
        assert!(provision(&mut bad, ids, true, |_| Ok(())).is_err());
        assert_eq!(bad.writes, 0);
    }

    #[test]
    fn interrupted_create_is_never_ordinary_assembly_and_resume_requires_survivors() {
        let base = Model::established();
        let ids = std::array::from_fn(|m| Record::decode(&base.cache[m][0]).unwrap().identity);
        let mut empty = base.clone();
        empty.cache = [[[0; 4096]; 3]; 3];
        empty.disk = empty.cache;
        let mut complete = empty.clone();
        provision(&mut complete, ids.clone(), false, |_| Ok(())).unwrap();
        let end = complete.calls;
        // Eighteen mutating boundaries: six slot writes/flushes, three seal writes/flushes.
        for failure in end - 17..=end {
            let mut m = empty.clone();
            m.fail = Some(failure);
            assert!(provision(&mut m, ids.clone(), false, |_| Ok(())).is_err());
            m.crash();
            m.fail = None;
            m.calls = 0;
            assert!(
                inspect(&mut m).is_err(),
                "partial creation incorrectly assembled boundary={failure}"
            );
            let survivors = m
                .cache
                .iter()
                .all(|member| member[..2].iter().any(|b| Record::decode(b).is_ok()));
            m.calls = 0;
            let resumed = provision(&mut m, ids.clone(), true, |_| Ok(()));
            assert_eq!(
                resumed.is_ok(),
                survivors,
                "resume wrong at boundary={failure}"
            );
            if survivors {
                assert!(!inspect(&mut m).unwrap().clean);
            }
        }
    }
}
