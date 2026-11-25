// SPDX-License-Identifier: GPL-2.0-or-later
//! Checked per-queue/tag scratch allocation. Kernel request pages are separate.
use anyhow::{bail, ensure, Context, Result};
const MAX_RING_ARENA: usize = 64 * 1024 * 1024;
pub const CHUNK: usize = 65536;
#[derive(Debug, Clone, Copy)]
pub struct BufferBudget {
    pub normal_ring_bytes: usize,
    pub sync_ring_bytes: usize,
    pub arena_bytes: usize,
    pub descriptor_bytes: usize,
    pub kernel_request_bytes: usize,
    depth: usize,
    sync_window: usize,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Slots {
    pub old_data: usize,
    pub old_parity: usize,
    pub new_parity: usize,
}
impl BufferBudget {
    pub fn new(queues: usize, depth: usize, sync_window: usize, limit: usize) -> Result<Self> {
        Self::with_request_size(queues, depth, sync_window, limit, 2 * CHUNK)
    }
    pub fn with_request_size(
        queues: usize,
        depth: usize,
        sync_window: usize,
        limit: usize,
        max_request: usize,
    ) -> Result<Self> {
        ensure!(
            max_request >= 2 * CHUNK && max_request % 4096 == 0,
            "invalid request memory bound"
        );
        ensure!(
            queues > 0 && depth > 0 && sync_window > 0,
            "empty queue or sync configuration"
        );
        let ring = |n: usize| -> Result<usize> {
            let bytes = n
                .checked_mul(3 * CHUNK)
                .context("arena multiplication overflow")?;
            ensure!(bytes <= MAX_RING_ARENA, "per-ring arena exceeds 64 MiB");
            Ok(bytes)
        };
        let normal_ring_bytes = ring(depth)?;
        let sync_ring_bytes = ring(sync_window)?;
        let arena_bytes = normal_ring_bytes
            .checked_mul(queues)
            .and_then(|b| b.checked_add(sync_ring_bytes))
            .context("total arena overflow")?;
        let tags = queues
            .checked_mul(depth)
            .and_then(|n| n.checked_add(sync_window))
            .context("descriptor count overflow")?;
        let descriptor_bytes = tags.checked_mul(64).context("descriptor byte overflow")?;
        ensure!(
            arena_bytes
                .checked_add(descriptor_bytes)
                .context("scratch budget overflow")?
                <= limit,
            "scratch budget exceeds configured limit"
        );
        let kernel_request_bytes = queues
            .checked_mul(depth)
            .and_then(|n| n.checked_mul(max_request))
            .context("request-page budget overflow")?;
        Ok(Self {
            normal_ring_bytes,
            sync_ring_bytes,
            arena_bytes,
            descriptor_bytes,
            kernel_request_bytes,
            depth,
            sync_window,
        })
    }
    pub fn normal_slots(&self, tag: usize) -> Result<Slots> {
        Self::slots(tag, self.depth)
    }
    pub fn sync_slots(&self, tag: usize) -> Result<Slots> {
        Self::slots(tag, self.sync_window)
    }
    fn slots(tag: usize, count: usize) -> Result<Slots> {
        if tag >= count {
            bail!("tag outside allocated arena");
        }
        let base = tag.checked_mul(3 * CHUNK).context("tag offset overflow")?;
        Ok(Slots {
            old_data: base,
            old_parity: base + CHUNK,
            new_parity: base + 2 * CHUNK,
        })
    }
}
#[cfg(test)]
#[path = "../../tests/raid5/buffer.rs"]
mod tests;
