// SPDX-License-Identifier: GPL-2.0-or-later
//! Whole NVMe ownership. Never explicitly unlock: io_uring file references must
//! keep the open-file-description flock alive through kernel resource teardown.
use super::{
    geometry::Geometry,
    metadata::{MetadataIo, RECORD_BYTES},
};
use anyhow::{ensure, Context, Result};
use std::{
    fs::{File, OpenOptions},
    os::{
        fd::AsRawFd,
        unix::fs::{FileExt, FileTypeExt, MetadataExt, OpenOptionsExt},
    },
    path::PathBuf,
};
pub struct Backends {
    files: [File; 3],
    pub paths: [PathBuf; 3],
    pub geometry: Geometry,
    pub sectors: [u32; 3],
    pub cache: [String; 3],
    startup_quiescent: bool,
    member_bytes: [u64; 3],
}
impl Backends {
    pub fn open(paths: [PathBuf; 3], test_files: bool) -> Result<Self> {
        let mut files = Vec::new();
        let mut sizes = Vec::new();
        let mut sectors = Vec::new();
        let mut caches = Vec::new();
        let mut identities = Vec::new();
        for path in &paths {
            let meta =
                std::fs::metadata(path).with_context(|| format!("stat {}", path.display()))?;
            let is_block = meta.file_type().is_block_device();
            ensure!(
                is_block || (test_files && meta.is_file()),
                "requires whole NVMe device (regular files only with explicit test-fixture mode)"
            );
            let key = if is_block {
                (1, meta.rdev(), 0)
            } else {
                (0, meta.dev(), meta.ino())
            };
            ensure!(!identities.contains(&key), "duplicate member identity");
            identities.push(key);
            let flags = libc::O_DIRECT | libc::O_CLOEXEC | if is_block { libc::O_EXCL } else { 0 };
            let file = OpenOptions::new()
                .read(true)
                .write(true)
                .custom_flags(flags)
                .open(path)
                .with_context(|| format!("exclusive direct open {}", path.display()))?;
            let opened = file.metadata()?;
            ensure!(
                opened.dev() == meta.dev()
                    && opened.ino() == meta.ino()
                    && opened.rdev() == meta.rdev()
                    && opened.file_type() == meta.file_type(),
                "member path changed during open"
            );
            // BSD flock is tied to struct file, not PID. No LOCK_UN is ever issued.
            ensure!(
                unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0,
                "member is still owned (including an old ring file reference): {}",
                path.display()
            );
            let (size, sector, cache) = if is_block {
                let sys = PathBuf::from(format!(
                    "/sys/dev/block/{}:{}",
                    libc::major(meta.rdev()),
                    libc::minor(meta.rdev())
                ));
                ensure!(
                    !sys.join("partition").exists(),
                    "partition members unsupported; use whole namespace"
                );
                ensure!(
                    sys.join("device/subsystem")
                        .canonicalize()?
                        .file_name()
                        .is_some_and(|n| n == "nvme"),
                    "backend is not NVMe"
                );
                ensure!(
                    std::fs::read_dir(sys.join("holders"))?.next().is_none(),
                    "NVMe has active holders"
                );
                let sector = std::fs::read_to_string(sys.join("queue/logical_block_size"))?
                    .trim()
                    .parse::<u32>()?;
                let physical = std::fs::read_to_string(sys.join("queue/physical_block_size"))?
                    .trim()
                    .parse::<u32>()?;
                ensure!(
                    (sector == 512 || sector == 4096)
                        && physical.is_power_of_two()
                        && physical >= sector
                        && physical <= 65536,
                    "unsupported LBA geometry"
                );
                let cache = std::fs::read_to_string(sys.join("queue/write_cache"))?
                    .trim()
                    .to_owned();
                ensure!(
                    cache == "write back" || cache == "write through",
                    "unknown write cache contract"
                );
                let mut bytes = 0u64;
                // BLKGETSIZE64 from pinned Linux UAPI, output is a u64.
                ensure!(
                    unsafe { libc::ioctl(file.as_raw_fd(), 0x80081272u64, &mut bytes) } == 0,
                    "BLKGETSIZE64 failed"
                );
                (bytes, sector, cache)
            } else {
                (meta.len(), 512, "test-file: fsync contract".to_owned())
            };
            files.push(file);
            sizes.push(size);
            sectors.push(sector);
            caches.push(cache);
        }
        ensure!(
            sectors.iter().all(|s| *s == sectors[0]),
            "members have different LBA sizes"
        );
        let member_bytes: [u64; 3] = sizes.try_into().unwrap();
        let geometry = Geometry::raid5(member_bytes)?;
        Ok(Self {
            files: files.try_into().unwrap(),
            paths,
            geometry,
            sectors: sectors.try_into().unwrap(),
            cache: caches.try_into().unwrap(),
            startup_quiescent: true,
            member_bytes,
        })
    }
    pub fn fds(&self) -> [i32; 3] {
        std::array::from_fn(|i| self.files[i].as_raw_fd())
    }
    pub fn member_bytes(&self) -> [u64; 3] {
        self.member_bytes
    }
    /// Must precede publishing the files into normal or sync rings. Metadata
    /// transitions are then forbidden until an explicit verified drain protocol.
    pub(crate) fn complete_drain(&mut self, _proof: &super::controller::Drained) {
        self.startup_quiescent = true;
    }
    pub fn begin_io(&mut self) {
        self.startup_quiescent = false;
    }
}
impl MetadataIo for Backends {
    fn persist_members(&mut self, updates: &[(usize, u64, [u8; RECORD_BYTES])]) -> Result<()> {
        ensure!(
            self.startup_quiescent,
            "metadata phase while queue ownership active"
        );
        let mut seen = [false; 3];
        for (m, offset, _) in updates {
            ensure!(
                *m < 3 && !seen[*m] && offset % 4096 == 0 && *offset <= 8192,
                "invalid/duplicate metadata phase"
            );
            seen[*m] = true;
        }
        std::thread::scope(|scope| {
            let mut workers = Vec::new();
            for (m, offset, record) in updates {
                let file = &self.files[*m];
                workers.push(scope.spawn(move || -> Result<()> {
                    let mut aligned = libublk::helpers::IoBuf::<u8>::new(RECORD_BYTES);
                    aligned.as_mut_slice().copy_from_slice(record);
                    let n = file.write_at(aligned.as_slice(), *offset)?;
                    ensure!(n == RECORD_BYTES, "short metadata phase write {n}");
                    file.sync_all()?;
                    Ok(())
                }));
            }
            // Consume ALL peers even if one failed. No second phase is issued
            // unless every member's first-slot write+flush succeeded.
            let mut error = None;
            for worker in workers {
                match worker.join() {
                    Ok(Ok(())) => (),
                    Ok(Err(e)) => {
                        error.get_or_insert(e);
                    }
                    Err(_) => {
                        error.get_or_insert(anyhow::anyhow!("metadata member worker panicked"));
                    }
                }
            }
            if let Some(e) = error {
                Err(e)
            } else {
                Ok(())
            }
        })
    }
    fn fence(&mut self) -> Result<()> {
        ensure!(
            self.startup_quiescent,
            "metadata owner has not proved queue/ring drain"
        );
        Ok(())
    }
    fn flush(&mut self, member: usize) -> Result<()> {
        self.files
            .get(member)
            .context("invalid member")?
            .sync_all()
            .context("member flush")
    }
    fn read(&mut self, member: usize, offset: u64) -> Result<[u8; RECORD_BYTES]> {
        ensure!(
            offset % 4096 == 0 && offset < super::geometry::PREFIX,
            "metadata read outside prefix"
        );
        let file = self.files.get(member).context("invalid member")?;
        let mut aligned = libublk::helpers::IoBuf::<u8>::new(RECORD_BYTES);
        let n = file.read_at(aligned.as_mut_slice(), offset)?;
        ensure!(n == RECORD_BYTES, "short metadata read {n}");
        Ok(aligned.as_slice().try_into().unwrap())
    }
    fn write(&mut self, member: usize, offset: u64, bytes: &[u8; RECORD_BYTES]) -> Result<()> {
        ensure!(
            self.startup_quiescent,
            "metadata write while queue ownership active"
        );
        ensure!(
            offset % 4096 == 0 && offset <= 8192,
            "metadata write outside known slots/seal"
        );
        let file = self.files.get(member).context("invalid member")?;
        let mut aligned = libublk::helpers::IoBuf::<u8>::new(RECORD_BYTES);
        aligned.as_mut_slice().copy_from_slice(bytes);
        let n = file.write_at(aligned.as_slice(), offset)?;
        ensure!(n == RECORD_BYTES, "short metadata write {n}");
        Ok(())
    }
}
