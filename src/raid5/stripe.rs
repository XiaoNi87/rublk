// SPDX-License-Identifier: GPL-2.0-or-later
//! Sharded exclusive stripe ownership; failed records live until device exit.
use super::flush_gate::{FlushGate, Permit};
use std::{
    collections::{HashMap, HashSet},
    sync::{Arc, Mutex, Weak},
};
struct Shard {
    gates: HashMap<u64, Weak<FlushGate>>,
    failed: HashSet<u64>,
}
struct Inner {
    shards: Vec<Mutex<Shard>>,
}
#[derive(Clone)]
pub struct StripeWorkTable {
    inner: Arc<Inner>,
}
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct FailedStripe(pub u64);
impl StripeWorkTable {
    pub fn new(shards: usize) -> Self {
        assert!(shards.is_power_of_two());
        Self {
            inner: Arc::new(Inner {
                shards: (0..shards)
                    .map(|_| {
                        Mutex::new(Shard {
                            gates: HashMap::new(),
                            failed: HashSet::new(),
                        })
                    })
                    .collect(),
            }),
        }
    }
    pub fn shard(&self, stripe: u64) -> usize {
        let hash = stripe.wrapping_mul(0x9e3779b97f4a7c15);
        ((hash ^ (hash >> 32)) as usize) & (self.inner.shards.len() - 1)
    }
    pub async fn acquire(&self, stripe: u64) -> Result<StripePermit, FailedStripe> {
        let index = self.shard(stripe);
        let gate = {
            let mut shard = super::metrics::lock(&self.inner.shards[index], true);
            // Reserve failure capacity BEFORE any transaction may issue writes.
            // HashSet allocation failure follows Rust's process-abort policy; never return success.
            let reserve = shard
                .gates
                .len()
                .checked_add(1)
                .expect("stripe count overflow");
            shard.failed.reserve(reserve);
            if shard.failed.contains(&stripe) {
                return Err(FailedStripe(stripe));
            }
            match shard.gates.get(&stripe).and_then(Weak::upgrade) {
                Some(g) => g,
                None => {
                    let g = Arc::new(FlushGate::default());
                    shard.gates.insert(stripe, Arc::downgrade(&g));
                    g
                }
            }
        };
        let lease = Lease {
            inner: self.inner.clone(),
            gate,
            stripe,
            index,
        };
        let timer = super::metrics::Timer::start();
        let permit = lease.gate.acquire_stripe().await;
        timer.record(super::metrics::Kind::StripeAdmission);
        // Failure publication precedes previous permit release. Mandatory post-grant check.
        let failed = super::metrics::lock(&self.inner.shards[index], true)
            .failed
            .contains(&stripe);
        if failed {
            drop(permit);
            drop(lease);
            return Err(FailedStripe(stripe));
        }
        Ok(StripePermit {
            permit: Some(permit),
            lease,
        })
    }
}
struct Lease {
    inner: Arc<Inner>,
    gate: Arc<FlushGate>,
    stripe: u64,
    index: usize,
}
impl Drop for Lease {
    fn drop(&mut self) {
        let mut shard = super::metrics::lock(&self.inner.shards[self.index], true);
        if Arc::strong_count(&self.gate) == 1 {
            shard.gates.remove(&self.stripe);
        }
    }
}
#[must_use]
pub struct StripePermit {
    permit: Option<Permit>,
    lease: Lease,
}
impl StripePermit {
    pub fn stripe(&self) -> u64 {
        self.lease.stripe
    }

    pub fn mark_failed(&self) {
        let mut shard = super::metrics::lock(&self.lease.inner.shards[self.lease.index], true);
        shard.failed.insert(self.lease.stripe);
    }
}
impl Drop for StripePermit {
    fn drop(&mut self) {
        drop(self.permit.take());
    }
}
#[cfg(test)]
#[path = "../../tests/raid5/stripe.rs"]
mod tests;
