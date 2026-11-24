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
