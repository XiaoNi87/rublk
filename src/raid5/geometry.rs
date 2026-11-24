// SPDX-License-Identifier: GPL-2.0-or-later
//! Checked left-symmetric mapping; no I/O and no metadata overlap.
use anyhow::{ensure, Context, Result};
pub const CHUNK: u64 = 65536;
pub const PREFIX: u64 = 1048576;
#[derive(Clone, Copy, Debug)]
pub struct Geometry {
    disks: u64,
    stripes: u64,
    capacity: u64,
}
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Segment {
    pub stripe: u64,
    pub data_disk: u64,
    pub parity_disk: u64,
    pub backend_offset: u64,
    pub request_offset: u64,
    pub len: u64,
}
impl Geometry {
    pub fn new(disks: u64, member_bytes: u64) -> Result<Self> {
        ensure!(disks >= 3, "at least three members");
        let data = member_bytes
            .checked_sub(PREFIX)
            .context("member smaller than metadata prefix")?;
        ensure!(
            member_bytes % 4096 == 0,
            "member size must be 4 KiB aligned"
        );
        let stripes = data / CHUNK;
        ensure!(stripes > 0, "no full stripe");
        let capacity = stripes
            .checked_mul(CHUNK)
            .and_then(|n| n.checked_mul(disks - 1))
            .context("logical capacity overflow")?;
        Ok(Self {
            disks,
            stripes,
            capacity,
        })
    }
    pub fn raid5(member_bytes: [u64; 3]) -> Result<Self> {
        ensure!(
            member_bytes.iter().all(|n| *n == member_bytes[0]),
            "members differ in size"
        );
        Self::new(3, member_bytes[0])
    }
    pub fn capacity(&self) -> u64 {
        self.capacity
    }
    pub fn stripes(&self) -> u64 {
        self.stripes
    }
    pub fn split(&self, offset: u64, len: u64) -> Result<Vec<Segment>> {
        ensure!(
            len > 0 && offset % 512 == 0 && len % 512 == 0,
            "invalid sector range"
        );
        ensure!(
            offset.checked_add(len).context("request overflow")? <= self.capacity,
            "request exceeds capacity"
        );
        let mut result = Vec::new();
        let mut done = 0;
        while done < len {
            let logical = offset + done;
            let chunk = logical / CHUNK;
            let stripe = chunk / (self.disks - 1);
            let order = chunk % (self.disks - 1);
            let parity_disk = self.disks - 1 - stripe % self.disks;
            let data_disk = (parity_disk + 1 + order) % self.disks;
            let within = logical % CHUNK;
            let bytes = (len - done).min(CHUNK - within);
            let backend_offset = PREFIX + stripe * CHUNK + within;
            result.push(Segment {
                stripe,
                data_disk,
                parity_disk,
                backend_offset,
                request_offset: done,
                len: bytes,
            });
            done += bytes;
        }
        Ok(result)
    }
}
