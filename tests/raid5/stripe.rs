// SPDX-License-Identifier: GPL-2.0-or-later
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
