// SPDX-License-Identifier: GPL-2.0-or-later
use super::*;
use futures::{task::noop_waker, FutureExt};
fn pending(a: &mut Admission) -> bool {
    Pin::new(a)
        .poll(&mut Context::from_waker(&noop_waker()))
        .is_pending()
}
#[test]
fn fifo_and_cancelled_grant() {
    let g = FlushGate::default();
    let a = g.acquire(Mode::Shared).now_or_never().unwrap();
    let b = g.acquire(Mode::Shared).now_or_never().unwrap();
    let flush = g.acquire(Mode::Exclusive);
    let mut late = g.acquire(Mode::Shared);
    assert!(pending(&mut late));
    drop(a);
    assert!(pending(&mut late));
    drop(b);
    // FLUSH granted, never polled; cancellation must hand off.
    drop(flush);
    let _late = late.now_or_never().unwrap();
}
#[test]
fn queued_cancel_and_multiple_flushes() {
    let g = FlushGate::default();
    let owner = g.acquire(Mode::Exclusive).now_or_never().unwrap();
    let cancel = g.acquire(Mode::Shared);
    let mut f1 = g.acquire(Mode::Exclusive);
    let mut f2 = g.acquire(Mode::Exclusive);
    drop(cancel);
    assert!(pending(&mut f1));
    drop(owner);
    let p = f1.now_or_never().unwrap();
    assert!(pending(&mut f2));
    drop(p);
    assert!(f2.now_or_never().is_some());
}
#[test]
fn wakes_after_unlock() {
    struct Reenter(FlushGate);
    impl futures::task::ArcWake for Reenter {
        fn wake_by_ref(a: &Arc<Self>) {
            assert!(a.0.inner.state.try_lock().is_ok());
        }
    }
    let g = FlushGate::default();
    let owner = g.acquire(Mode::Exclusive).now_or_never().unwrap();
    let mut waiting = g.acquire(Mode::Shared);
    let w = futures::task::waker(Arc::new(Reenter(g.clone())));
    assert!(Pin::new(&mut waiting)
        .poll(&mut Context::from_waker(&w))
        .is_pending());
    drop(owner);
    assert!(waiting.now_or_never().is_some());
}
#[test]
fn queued_flush_not_starved() {
    let g = FlushGate::default();
    let owner = g.acquire(Mode::Shared).now_or_never().unwrap();
    let flush = g.acquire(Mode::Exclusive);
    let mut later: Vec<_> = (0..1000).map(|_| g.acquire(Mode::Shared)).collect();
    drop(owner);
    for a in &mut later {
        assert!(pending(a));
    }
    let p = flush.now_or_never().unwrap();
    drop(p);
    let permits: Vec<_> = later
        .into_iter()
        .map(|a| a.now_or_never().unwrap())
        .collect();
    assert_eq!(permits.len(), 1000);
}
