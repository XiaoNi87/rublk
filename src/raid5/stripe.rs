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
mod tests {
    use super::*;
    use futures::{task::noop_waker, FutureExt};
    use std::{future::Future, task::Context};
    #[test]
    fn failure_rechecked_after_grant() {
        let t = StripeWorkTable::new(64);
        let a = t.acquire(9).now_or_never().unwrap().unwrap();
        let mut b = Box::pin(t.acquire(9));
        assert!(b
            .as_mut()
            .poll(&mut Context::from_waker(&noop_waker()))
            .is_pending());
        a.mark_failed();
        drop(a);
        assert!(matches!(b.now_or_never(), Some(Err(FailedStripe(9)))));
        assert!(t.inner.shards[t.shard(9)].lock().unwrap().gates.is_empty());
        assert!(matches!(
            t.acquire(9).now_or_never(),
            Some(Err(FailedStripe(9)))
        ));
    }
    #[test]
    fn independent_stripes_and_queued_cancel() {
        let t = StripeWorkTable::new(64);
        let a = t.acquire(1).now_or_never().unwrap().unwrap();
        let other = t.acquire(2).now_or_never().unwrap().unwrap();
        let mut b = Box::pin(t.acquire(1));
        assert!(b
            .as_mut()
            .poll(&mut Context::from_waker(&noop_waker()))
            .is_pending());
        drop(b);
        drop(a);
        let _new = t.acquire(1).now_or_never().unwrap().unwrap();
        drop(other);
    }
    #[test]
    fn across_threads_exclusive() {
        use std::sync::atomic::{AtomicUsize, Ordering};
        let t = StripeWorkTable::new(64);
        let active = Arc::new(AtomicUsize::new(0));
        std::thread::scope(|scope| {
            for _ in 0..4 {
                let t = t.clone();
                let active = active.clone();
                scope.spawn(move || {
                    smol::block_on(async {
                        for _ in 0..1000 {
                            let _p = t.acquire(17).await.unwrap();
                            assert_eq!(active.fetch_add(1, Ordering::SeqCst), 0);
                            std::thread::yield_now();
                            assert_eq!(active.fetch_sub(1, Ordering::SeqCst), 1);
                        }
                    })
                });
            }
        });
        assert!(t.inner.shards[t.shard(17)].lock().unwrap().gates.is_empty());
    }
}
