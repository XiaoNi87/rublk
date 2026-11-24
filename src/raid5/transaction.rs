// SPDX-License-Identifier: GPL-2.0-or-later
//! Queue-local ownership. Dropping a result waiter only signals cancellation.
//! Backend helpers MUST consume each original CQE before returning, including errors.
use anyhow::{ensure, Result};
use futures::channel::oneshot;
use std::{
    cell::Cell,
    future::Future,
    pin::Pin,
    rc::Rc,
    task::{Context, Poll},
};
/// Only this typed error denotes cancellation before submitting any I/O.
#[derive(Debug, thiserror::Error)]
#[error("transaction cancelled before I/O")]
pub struct BeforeIoCancelled;
struct CancelState {
    requested: Cell<bool>,
    wake: futures::task::AtomicWaker,
}
#[derive(Clone)]
pub struct Cancellation(Rc<CancelState>);
impl Cancellation {
    pub fn requested(&self) -> bool {
        self.0.requested.get()
    }
    pub fn check_before_io(&self) -> Result<()> {
        if self.requested() {
            Err(BeforeIoCancelled.into())
        } else {
            Ok(())
        }
    }
    fn signal(&self) {
        self.0.requested.set(true);
        self.0.wake.wake();
    }
    pub async fn cancelled(&self) {
        futures::future::poll_fn(|cx| {
            self.0.wake.register(cx.waker());
            if self.requested() {
                Poll::Ready(())
            } else {
                Poll::Pending
            }
        })
        .await
    }
    /// Only for admission/permit waits BEFORE any I/O is submitted. Never wrap
    /// an original backend/BPF CQE future in this cancellable wait.
    pub async fn before_io<F: Future>(
        &self,
        wake: &super::queue_wake::QueueWake,
        future: F,
    ) -> Result<F::Output> {
        let cancelled = self.cancelled();
        futures::pin_mut!(future, cancelled);
        match wake.wait(futures::future::select(future, cancelled)).await {
            futures::future::Either::Left((result, _)) => {
                self.check_before_io()?;
                Ok(result)
            }
            futures::future::Either::Right(_) => Err(BeforeIoCancelled.into()),
        }
    }
}
struct Entry {
    task: smol::Task<()>,
    cancel: Cancellation,
}
#[must_use = "queue owner must explicitly drain transactions before teardown"]
pub struct Registry<'executor, 'task> {
    entries: Vec<Option<Entry>>,
    accepting: bool,
    executor: &'executor smol::LocalExecutor<'task>,
}
impl<'executor, 'task> Registry<'executor, 'task> {
    pub fn new(depth: usize, executor: &'executor smol::LocalExecutor<'task>) -> Result<Self> {
        ensure!(
            depth > 0 && depth <= u16::MAX as usize,
            "invalid transaction depth"
        );
        Ok(Self {
            entries: (0..depth).map(|_| None).collect(),
            accepting: true,
            executor,
        })
    }
    pub fn spawn<F, M>(&mut self, tag: u16, make: M) -> Result<Receipt>
    where
        F: Future<Output = Result<i32>> + 'task,
        M: FnOnce(Cancellation) -> F,
    {
        self.reap();
        ensure!(self.accepting, "transaction admission closed");
        let slot = self
            .entries
            .get_mut(tag as usize)
            .ok_or_else(|| anyhow::anyhow!("tag out of range"))?;
        ensure!(slot.is_none(), "tag already owned");
        let cancellation = Cancellation(Rc::new(CancelState {
            requested: Cell::new(false),
            wake: futures::task::AtomicWaker::new(),
        }));
        let future = make(cancellation.clone());
        let (send, receive) = oneshot::channel();
        let task = self.executor.spawn(async move {
            let result = future.await;
            let _ = send.send(result);
        });
        *slot = Some(Entry {
            task,
            cancel: cancellation.clone(),
        });
        Ok(Receipt {
            receive,
            cancel: cancellation,
        })
    }
    pub fn reap(&mut self) {
        for slot in &mut self.entries {
            if slot.as_ref().is_some_and(|e| e.task.is_finished()) {
                drop(slot.take());
            }
        }
    }
    pub fn active(&self) -> usize {
        self.entries
            .iter()
            .filter(|e| e.as_ref().is_some_and(|e| !e.task.is_finished()))
            .count()
    }
    pub fn close(&mut self, cancel: bool) {
        self.accepting = false;
        if cancel {
            for e in self.entries.iter().flatten() {
                e.cancel.signal();
            }
        }
    }
    pub async fn drain(&mut self) {
        self.close(true);
        for slot in &mut self.entries {
            if let Some(entry) = slot.take() {
                entry.task.await;
            }
        }
    }
}
impl Drop for Registry<'_, '_> {
    fn drop(&mut self) {
        // Synchronous Drop cannot drain a CQE. Never abort owner futures and release
        // permits while their kernel writes remain alive. Emergency fail-stop leaves
        // metadata DIRTY; normal queue shutdown must use drain() first.
        if self.active() != 0 {
            super::log(format_args!(
                "fatal: RAID5 registry dropped before original CQE drain"
            ));
            std::process::abort();
        }
    }
}
#[must_use]
pub struct Receipt {
    receive: oneshot::Receiver<Result<i32>>,
    cancel: Cancellation,
}
impl Future for Receipt {
    type Output = Result<i32>;
    fn poll(mut self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        match Pin::new(&mut self.receive).poll(cx) {
            Poll::Ready(Ok(r)) => Poll::Ready(r),
            Poll::Ready(Err(e)) => Poll::Ready(Err(e.into())),
            Poll::Pending => Poll::Pending,
        }
    }
}
impl Drop for Receipt {
    fn drop(&mut self) {
        self.cancel.signal();
    }
}
#[cfg(test)]
mod tests {
    use super::*;
    use crate::raid5::{
        flush_gate::{FlushGate, Mode},
        stripe::{FailedStripe, StripeWorkTable},
    };
    use futures::FutureExt;
    #[test]
    fn cancelled_waiter_does_not_drop_transaction_or_permit() {
        let executor = smol::LocalExecutor::new();
        let mut registry = Registry::new(2, &executor).unwrap();
        let stripes = StripeWorkTable::new(64);
        let gate = FlushGate::default();
        let (send, cqe) = oneshot::channel::<()>();
        let t = stripes.clone();
        let g = gate.clone();
        let receipt = registry
            .spawn(0, move |cancel| async move {
                let _admission = g.acquire(Mode::Shared).await;
                let stripe = t.acquire(0).await.unwrap();
                cqe.await?;
                if cancel.requested() {
                    stripe.mark_failed();
                }
                Ok(-libc::EIO)
            })
            .unwrap();
        while executor.try_tick() {}
        drop(receipt);
        assert_eq!(registry.active(), 1);
        assert!(stripes.acquire(0).now_or_never().is_none());
        assert!(gate.acquire(Mode::Exclusive).now_or_never().is_none());
        send.send(()).unwrap();
        while executor.try_tick() {}
        assert_eq!(registry.active(), 0);
        registry.reap();
        assert!(matches!(
            stripes.acquire(0).now_or_never(),
            Some(Err(FailedStripe(0)))
        ));
        assert!(gate.acquire(Mode::Exclusive).now_or_never().is_some());
        smol::block_on(registry.drain());
    }
    #[test]
    fn duplicate_tag_and_closed_admission_rejected() {
        let executor = smol::LocalExecutor::new();
        let mut r = Registry::new(1, &executor).unwrap();
        let (send, cqe) = oneshot::channel();
        let receipt = r
            .spawn(0, |_| async {
                cqe.await?;
                Ok(0)
            })
            .unwrap();
        assert!(r.spawn(0, |_| async { Ok(0) }).is_err());
        r.close(true);
        assert!(r.spawn(0, |_| async { Ok(0) }).is_err());
        send.send(()).unwrap();
        while executor.try_tick() {}
        assert_eq!(smol::block_on(receipt).unwrap(), 0);
        smol::block_on(r.drain());
    }
}
