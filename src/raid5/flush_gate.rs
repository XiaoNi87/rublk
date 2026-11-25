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
#[path = "../../tests/raid5/flush_gate.rs"]
mod tests;
