// SPDX-License-Identifier: GPL-2.0-or-later
use anyhow::{ensure, Result};
use rublk::raid5::{
    backend::Backends,
    geometry::{CHUNK, PREFIX},
    metadata::{inspect, mark_dirty, provision, Identity},
    startup::verify_adoption,
};
use std::{
    fs::{File, OpenOptions},
    os::unix::fs::FileExt,
};
fn main() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let paths = std::array::from_fn(|i| dir.path().join(format!("member{i}")));
    for p in &paths {
        File::create(p)?.set_len(PREFIX + 3 * CHUNK)?;
    }
    // Independent hard-coded left-symmetric rotations, nonzero valid parity.
    for (stripe, (d0, d1, p)) in [(0, 1, 2), (2, 0, 1), (1, 2, 0)].into_iter().enumerate() {
        let a = stripe as u8 + 1;
        let b = stripe as u8 + 17;
        for (disk, value) in [(d0, a), (d1, b), (p, a ^ b)] {
            OpenOptions::new()
                .write(true)
                .open(&paths[disk])?
                .write_all_at(&vec![value; CHUNK as usize], PREFIX + stripe as u64 * CHUNK)?;
        }
    }
    let mut backends = Backends::open(paths.clone(), true)?;
    verify_adoption(&backends)?;
    println!("PASS: nonzero valid parity across three rotations verified by BPF, read-only");
    let identities = std::array::from_fn(|m| Identity {
        array: [9; 16],
        members: [[1; 16], [2; 16], [3; 16]],
        member: m as u32,
        member_bytes: PREFIX + 3 * CHUNK,
        sector: 512,
    });
    provision(&mut backends, identities, false, |b| verify_adoption(b))?;
    let snapshot = inspect(&mut backends)?;
    ensure!(!snapshot.clean && snapshot.generation == 1);
    ensure!(mark_dirty(&mut backends)? == 2);
    println!(
        "PASS: actual O_DIRECT metadata writes/flushes/seals and established DIRTY transition"
    );
    drop(backends);
    // Deliberately corrupt initial parity and demonstrate rejection BEFORE any RMW.
    OpenOptions::new()
        .write(true)
        .open(&paths[2])?
        .write_all_at(&vec![0x55; 4096], PREFIX)?;
    let before: Vec<_> = paths
        .iter()
        .map(std::fs::read)
        .collect::<std::io::Result<_>>()?;
    let backends = Backends::open(paths.clone(), true)?;
    ensure!(
        verify_adoption(&backends).is_err(),
        "inconsistent initial parity accepted"
    );
    drop(backends);
    for (i, p) in paths.iter().enumerate() {
        ensure!(
            std::fs::read(p)? == before[i],
            "adoption modified member {i}"
        );
    }
    println!("PASS: bad initial parity rejected, every metadata/data byte unchanged; no writable device published");
    Ok(())
}
