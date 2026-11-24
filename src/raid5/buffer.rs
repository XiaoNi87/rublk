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
mod tests {
    use super::*;
    #[test]
    fn all_slots_are_disjoint_and_fit_exact_allocation() {
        for depth in [1, 8, 32, 128, 256] {
            let b = BufferBudget::new(4, depth, 32, 1024 * 1024 * 1024).unwrap();
            let mut ends = Vec::new();
            for tag in 0..depth {
                let s = b.normal_slots(tag).unwrap();
                for start in [s.old_data, s.old_parity, s.new_parity] {
                    ends.push((start, start + CHUNK));
                }
            }
            ends.sort_unstable();
            assert_eq!(ends.first().unwrap().0, 0);
            assert!(ends.windows(2).all(|w| w[0].1 == w[1].0));
            assert_eq!(ends.last().unwrap().1, b.normal_ring_bytes);
            assert!(b.normal_slots(depth).is_err());
            assert_eq!(
                b.sync_slots(31).unwrap().new_parity + CHUNK,
                b.sync_ring_bytes
            );
            assert!(b.sync_slots(32).is_err());
        }
    }
    #[test]
    fn budgets_and_overflow_are_rejected_before_allocation() {
        let b = BufferBudget::new(4, 128, 32, 1 << 30).unwrap();
        assert_eq!(b.arena_bytes, 106954752);
        assert_eq!(b.kernel_request_bytes, 67108864);
        assert_eq!(b.descriptor_bytes, 34816);
        assert!(BufferBudget::new(0, 1, 1, usize::MAX).is_err());
        assert!(BufferBudget::new(1, 0, 1, usize::MAX).is_err());
        assert!(BufferBudget::new(1, 1, 0, usize::MAX).is_err());
        assert!(BufferBudget::new(usize::MAX, 1, 1, usize::MAX).is_err());
        assert!(BufferBudget::new(1, usize::MAX, 1, usize::MAX).is_err());
        assert!(BufferBudget::new(1, 342, 1, usize::MAX).is_err());
        assert!(BufferBudget::new(4, 128, 32, b.arena_bytes + b.descriptor_bytes - 1).is_err());
    }
}
