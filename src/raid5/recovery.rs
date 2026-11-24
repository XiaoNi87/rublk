// SPDX-License-Identifier: GPL-2.0-or-later
//! Volatile contiguous frontier. A new dirty session always starts at zero.
use anyhow::{ensure, Result};
use std::collections::BTreeSet;
pub struct Frontier {
    total: u64,
    window: u64,
    next: u64,
    completed: BTreeSet<u64>,
}
impl Frontier {
    pub fn new(total: u64, window: u64) -> Result<Self> {
        ensure!(total > 0 && window > 0, "empty sync configuration");
        Ok(Self {
            total,
            window,
            next: 0,
            completed: BTreeSet::new(),
        })
    }
    pub fn clean_baseline(total: u64, window: u64) -> Result<Self> {
        let mut f = Self::new(total, window)?;
        f.next = total;
        Ok(f)
    }
    pub fn first_unsynced(&self) -> u64 {
        self.next
    }
    pub fn done(&self) -> bool {
        self.next == self.total
    }
    /// Scheduler may launch only within this bounded interval, after obtaining
    /// shared flush admission then a stripe permit and rechecking failed/frontier.
    pub fn schedulable(&self, stripe: u64) -> bool {
        stripe >= self.next
            && stripe < self.total
            && stripe - self.next < self.window
            && !self.completed.contains(&stripe)
    }
    pub fn complete(&mut self, stripe: u64) -> Result<()> {
        // Invoke only after exact full parity write CQE while still holding permit.
        ensure!(stripe < self.total, "stripe exceeds capacity");
        if stripe < self.next {
            return Ok(());
        }
        // Foreground repair ahead of the bounded background window need not be
        // recorded: it is safe for the worker to sync that stripe again later.
        if stripe - self.next >= self.window {
            return Ok(());
        }
        self.completed.insert(stripe);
        while self.completed.remove(&self.next) {
            self.next += 1;
        }
        Ok(())
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn only_contiguous_completed_range_advances() {
        let mut f = Frontier::new(100, 32).unwrap();
        f.complete(2).unwrap();
        f.complete(1).unwrap();
        assert_eq!(f.first_unsynced(), 0);
        f.complete(0).unwrap();
        assert_eq!(f.first_unsynced(), 3);
        assert!(!f.schedulable(35));
        assert!(f.schedulable(34));
    }
    #[test]
    fn far_repair_bounded_and_restart_from_zero() {
        let mut f = Frontier::new(1000000, 32).unwrap();
        for i in 32..1000000 {
            f.complete(i).unwrap();
        }
        assert!(f.completed.is_empty());
        for i in (0..32).rev() {
            f.complete(i).unwrap();
        }
        assert_eq!(f.first_unsynced(), 32);
        assert_eq!(Frontier::new(1000000, 32).unwrap().first_unsynced(), 0);
    }
    #[test]
    fn frontier_never_clears_failed_or_proves_durable_clean() {
        let mut f = Frontier::new(1, 1).unwrap();
        f.complete(0).unwrap();
        assert!(f.done());
        assert!(f.complete(1).is_err());
        assert!(!f.schedulable(0));
    }
}
