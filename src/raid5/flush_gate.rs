// SPDX-License-Identifier: GPL-2.0-or-later
//! FIFO async admission. Bookkeeping locks never run user wake callbacks.
use futures::task::AtomicWaker;
use std::{
    collections::VecDeque,
    future::Future,
    pin::Pin,
    sync::{
        atomic::{AtomicU8, Ordering},
        Arc, Mutex,
    },
    task::{Context, Poll},
};
const QUEUED: u8 = 0;
const GRANTED: u8 = 1;
const RELEASED: u8 = 2;
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Shared,
    Exclusive,
}
struct Waiter {
    mode: Mode,
    state: AtomicU8,
    wake: AtomicWaker,
}
#[derive(Default)]
struct State {
    readers: usize,
    writer: bool,
    queue: VecDeque<Arc<Waiter>>,
}
#[derive(Default)]
struct Inner {
    state: Mutex<State>,
}
#[derive(Clone, Default)]
pub struct FlushGate {
    inner: Arc<Inner>,
}
impl Inner {
    // Grant and release linearize under state; notification runs after unlock.
    fn grant(s: &mut State) -> Vec<Arc<Waiter>> {
        let mut ready = Vec::new();
        while !s.writer {
            let Some(front) = s.queue.front() else { break };
            if front.mode == Mode::Exclusive && s.readers != 0 {
                break;
            }
            let next = s.queue.pop_front().unwrap();
            match next.mode {
                Mode::Shared => s.readers += 1,
                Mode::Exclusive => s.writer = true,
            }
            next.state.store(GRANTED, Ordering::Release);
            ready.push(next);
        }
        ready
    }
    fn notify(ready: Vec<Arc<Waiter>>) {
        for node in ready {
            node.wake.wake();
        }
    }
    fn release(&self, node: &Arc<Waiter>) {
        let ready = {
            let mut s = super::metrics::lock(&self.state, false);
            match node.state.swap(RELEASED, Ordering::AcqRel) {
                QUEUED => {
                    let i = s
                        .queue
                        .iter()
                        .position(|n| Arc::ptr_eq(n, node))
                        .expect("queued token exists");
                    s.queue.remove(i);
                }
                GRANTED => match node.mode {
                    Mode::Shared => s.readers -= 1,
                    Mode::Exclusive => s.writer = false,
                },
                _ => panic!("admission released twice"),
            }
            Self::grant(&mut s)
        };
        Self::notify(ready);
    }
}
impl FlushGate {
    /// Enqueue before returning the future; dropping it cancels even if never polled.
    pub fn acquire(&self, mode: Mode) -> Admission {
        self.acquire_inner(mode, true)
    }
    pub(crate) fn acquire_stripe(&self) -> Admission {
        self.acquire_inner(Mode::Exclusive, false)
    }
    fn acquire_inner(&self, mode: Mode, measured: bool) -> Admission {
        let timer = super::metrics::Timer::start();
        let node = Arc::new(Waiter {
            mode,
            state: AtomicU8::new(QUEUED),
            wake: AtomicWaker::new(),
        });
        let ready = {
            let mut s = super::metrics::lock(&self.inner.state, false);
            s.queue.push_back(node.clone());
            Inner::grant(&mut s)
        };
        Inner::notify(ready);
        Admission {
            inner: self.inner.clone(),
            node: Some(node),
            timer: measured.then_some(timer),
        }
    }
}
#[must_use]
pub struct Admission {
    inner: Arc<Inner>,
    node: Option<Arc<Waiter>>,
    timer: Option<super::metrics::Timer>,
}
impl Future for Admission {
    type Output = Permit;
    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Permit> {
        let node = self.node.as_ref().expect("polled completed admission");
        // Register before observing the state: no grant-to-register lost wake.
        node.wake.register(cx.waker());
        if node.state.load(Ordering::Acquire) == GRANTED {
            let kind = if node.mode == Mode::Shared {
                super::metrics::Kind::SharedAdmission
            } else {
                super::metrics::Kind::ExclusiveAdmission
            };
            if let Some(timer) = self.timer.take() {
                timer.record(kind);
            }
            Poll::Ready(Permit {
                inner: self.inner.clone(),
                node: self.node.take().unwrap(),
            })
        } else {
            Poll::Pending
        }
    }
}
impl Drop for Admission {
    fn drop(&mut self) {
        if let Some(node) = self.node.take() {
            self.inner.release(&node);
        }
    }
}
/// Owning transaction must retain this until every submitted CQE is consumed.
#[must_use]
pub struct Permit {
    inner: Arc<Inner>,
    node: Arc<Waiter>,
}
impl Drop for Permit {
    fn drop(&mut self) {
        self.inner.release(&self.node);
    }
}
#[cfg(test)]
mod tests {
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
}
