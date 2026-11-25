// SPDX-License-Identifier: GPL-2.0-or-later
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
                let ids =
                    std::array::from_fn(|m| Record::decode(&base.cache[m][0]).unwrap().identity);
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
                let ids =
                    std::array::from_fn(|m| Record::decode(&base.cache[m][0]).unwrap().identity);
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
    fn enumerate(state: [usize; 3], prefix: Vec<(usize, bool)>, out: &mut Vec<Vec<(usize, bool)>>) {
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
    let ids = std::array::from_fn(|member| Record::decode(&m.cache[member][0]).unwrap().identity);
    m.cache[1][2] = [0; 4096];
    m.fail = Some(2);
    assert!(provision(&mut m, ids, true, |_| Ok(())).is_err());
    assert_eq!(m.writes, 0);
}
#[test]
fn resume_repairs_missing_seal_only_with_all_original_identities() {
    let mut m = Model::established();
    let ids = std::array::from_fn(|member| Record::decode(&m.cache[member][0]).unwrap().identity);
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
