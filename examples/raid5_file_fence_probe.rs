// SPDX-License-Identifier: GPL-2.0-or-later
//! A registered file keeps the OFD flock after all original userspace fds close.
use anyhow::{ensure, Result};
use std::{fs::OpenOptions, os::fd::AsRawFd};
fn locked(file: &std::fs::File) -> bool {
    unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) == 0 }
}
fn main() -> Result<()> {
    let dir = tempfile::tempdir()?;
    let path = dir.path().join("member");
    let old = OpenOptions::new()
        .create_new(true)
        .read(true)
        .write(true)
        .open(&path)?;
    ensure!(locked(&old));
    let ring = io_uring::IoUring::new(8)?;
    ring.submitter().register_files(&[old.as_raw_fd()])?;
    drop(old);
    let new = OpenOptions::new().read(true).write(true).open(&path)?;
    ensure!(
        !locked(&new),
        "lock disappeared while kernel fixed table retained old file"
    );
    ring.submitter().unregister_files()?;
    // fput may be deferred: bound retry rather than treating CQE/close as final.
    let until = std::time::Instant::now() + std::time::Duration::from_secs(5);
    while !locked(&new) {
        ensure!(
            std::time::Instant::now() < until,
            "old file reference not released"
        );
        std::thread::yield_now();
    }
    println!("PASS: old kernel fixed-file reference retains member flock; takeover succeeds only after final release");
    Ok(())
}
