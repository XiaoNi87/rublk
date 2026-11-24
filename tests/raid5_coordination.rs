// SPDX-License-Identifier: GPL-2.0-or-later
#![cfg(feature = "raid5")]
use futures::{task::noop_waker, FutureExt};
use rublk::raid5::{
    flush_gate::{FlushGate, Mode},
    recovery::Frontier,
    stripe::{FailedStripe, StripeWorkTable},
};
use std::{future::Future, pin::Pin, task::Context};
fn pending<F: Future>(future: Pin<&mut F>) -> bool {
    future
        .poll(&mut Context::from_waker(&noop_waker()))
        .is_pending()
}
#[test]
fn sync_rmw_flush_and_later_writer_obey_one_order() {
    let gate = FlushGate::default();
    let stripes = StripeWorkTable::new(64);
    let sync_admission = gate.acquire(Mode::Shared).now_or_never().unwrap();
    let sync = stripes.acquire(1).now_or_never().unwrap().unwrap();
    let write_admission = gate.acquire(Mode::Shared).now_or_never().unwrap();
    let mut rmw = Box::pin(stripes.acquire(1));
    assert!(pending(rmw.as_mut()));
    let next_sync_admission = gate.acquire(Mode::Shared).now_or_never().unwrap();
    let mut flush = Box::pin(gate.acquire(Mode::Exclusive));
    let mut later = Box::pin(gate.acquire(Mode::Shared));
    assert!(pending(flush.as_mut()));
    assert!(pending(later.as_mut()));
    // Only original sync CQE drain permits handoff, then admission release.
    drop(sync);
    drop(sync_admission);
    let rmw = rmw.now_or_never().unwrap().unwrap();
    let mut next_sync = Box::pin(stripes.acquire(1));
    assert!(pending(next_sync.as_mut()));
    // Repair -> RMW retains the exact same permit/admission; neither FLUSH nor
    // background sync may observe an intermediate phase.
    assert!(pending(flush.as_mut()));
    assert!(pending(next_sync.as_mut()));
    drop(rmw);
    drop(write_admission);
    let next_sync = next_sync.now_or_never().unwrap().unwrap();
    drop(next_sync);
    drop(next_sync_admission);
    let flush = flush.now_or_never().unwrap();
    assert!(pending(later.as_mut()));
    drop(flush);
    assert!(later.now_or_never().is_some());
}
#[test]
fn failed_state_overrides_completed_frontier_for_read_and_sync() {
    let stripes = StripeWorkTable::new(64);
    let mut frontier = Frontier::new(3, 3).unwrap();
    let owner = stripes.acquire(0).now_or_never().unwrap().unwrap();
    let mut read = Box::pin(stripes.acquire(0));
    let mut sync = Box::pin(stripes.acquire(0));
    assert!(pending(read.as_mut()));
    assert!(pending(sync.as_mut()));
    frontier.complete(0).unwrap();
    assert_eq!(frontier.first_unsynced(), 1);
    owner.mark_failed();
    drop(owner);
    assert!(matches!(read.now_or_never(), Some(Err(FailedStripe(0)))));
    assert!(matches!(sync.now_or_never(), Some(Err(FailedStripe(0)))));
}
#[test]
fn foreground_repair_frontier_rechecked_under_stripe_ownership() {
    let stripes = StripeWorkTable::new(64);
    let mut frontier = Frontier::new(2, 2).unwrap();
    let write = stripes.acquire(0).now_or_never().unwrap().unwrap();
    let mut sync = Box::pin(stripes.acquire(0));
    assert!(pending(sync.as_mut()));
    assert_eq!(frontier.first_unsynced(), 0);
    // Foreground repairs parity before RMW; frontier changes while sync waits.
    frontier.complete(0).unwrap();
    drop(write);
    let sync = sync.now_or_never().unwrap().unwrap();
    assert!(!frontier.schedulable(0));
    drop(sync);
}

#[test]
fn cancellation_in_each_submitted_phase_retains_stripe_and_flush_admission() {
    use futures::channel::oneshot;
    use rublk::raid5::transaction::Registry;
    use std::{cell::Cell, rc::Rc};
    for cancel_phase in 0..3 {
        let executor = smol::LocalExecutor::new();
        let mut registry = Registry::new(1, &executor).unwrap();
        let stripes = StripeWorkTable::new(64);
        let gate = FlushGate::default();
        let stage = Rc::new(Cell::new(0));
        let (senders, completions): (Vec<_>, Vec<_>) =
            (0..3).map(|_| oneshot::channel::<()>()).unzip();
        let t = stripes.clone();
        let g = gate.clone();
        let s = stage.clone();
        let receipt = registry
            .spawn(0, move |_| async move {
                let _admission = g.acquire(Mode::Shared).await;
                let permit = t.acquire(0).await.unwrap();
                // Model original read, BPF, then write CQEs. Dropped result waiter
                // must not abort any of these futures or free their shared resources.
                for (i, cqe) in completions.into_iter().enumerate() {
                    s.set(i + 1);
                    cqe.await?;
                }
                permit.mark_failed();
                Ok(-libc::EIO)
            })
            .unwrap();
        while executor.try_tick() {}
        let mut receipt = Some(receipt);
        let mut successor = Box::pin(stripes.acquire(0));
        let mut flush = Box::pin(gate.acquire(Mode::Exclusive));
        for (phase, send) in senders.into_iter().enumerate() {
            while executor.try_tick() {}
            assert_eq!(stage.get(), phase + 1);
            if phase == cancel_phase {
                drop(receipt.take());
            }
            assert!(pending(successor.as_mut()));
            assert!(pending(flush.as_mut()));
            assert_eq!(registry.active(), 1);
            // A cancellation acknowledgement alone is deliberately not an
            // original completion. Only resolving this original channel advances.
            send.send(()).unwrap();
        }
        while executor.try_tick() {}
        assert_eq!(registry.active(), 0);
        assert!(matches!(
            successor.now_or_never(),
            Some(Err(FailedStripe(0)))
        ));
        assert!(flush.now_or_never().is_some());
        smol::block_on(registry.drain());
    }
}

#[test]
fn cancelled_flush_waiter_keeps_exclusive_gate_until_all_member_fsync_cqes() {
    use futures::channel::oneshot;
    use rublk::raid5::transaction::Registry;
    let executor = smol::LocalExecutor::new();
    let mut registry = Registry::new(1, &executor).unwrap();
    let gate = FlushGate::default();
    let g = gate.clone();
    let (senders, completions): (Vec<_>, Vec<_>) = (0..3).map(|_| oneshot::channel::<()>()).unzip();
    let receipt = registry
        .spawn(0, move |_| async move {
            let _permit = g.acquire(Mode::Exclusive).await;
            for original in completions {
                original.await?;
            }
            Ok(0)
        })
        .unwrap();
    while executor.try_tick() {}
    drop(receipt);
    let mut writer = Box::pin(gate.acquire(Mode::Shared));
    registry.close(true);
    for send in senders {
        assert!(pending(writer.as_mut()));
        assert_eq!(registry.active(), 1);
        send.send(()).unwrap();
        while executor.try_tick() {}
    }
    assert!(writer.now_or_never().is_some());
    assert_eq!(registry.active(), 0);
    smol::block_on(registry.drain());
}

#[test]
fn cancel_before_grant_and_after_unpolled_grant_never_submits_io() {
    use rublk::raid5::{queue_wake::QueueWake, transaction::Registry};
    use std::{cell::Cell, rc::Rc};
    for grant_first in [false, true] {
        let executor = smol::LocalExecutor::new();
        let mut registry = Registry::new(1, &executor).unwrap();
        let gate = FlushGate::default();
        let stripes = StripeWorkTable::new(1);
        let blocker = stripes.acquire(7).now_or_never().unwrap().unwrap();
        let submitted = Rc::new(Cell::new(0));
        let wake = QueueWake::new().unwrap();
        let (g, t, count) = (gate.clone(), stripes.clone(), submitted.clone());
        let receipt = registry
            .spawn(0, move |cancel| async move {
                let _admission = g.acquire(Mode::Shared).await;
                let _permit = cancel
                    .before_io(&wake, t.acquire(7))
                    .await?
                    .map_err(|_| anyhow::anyhow!("failed stripe"))?;
                count.set(count.get() + 1);
                Ok(0)
            })
            .unwrap();
        while executor.try_tick() {}
        let mut blocker = Some(blocker);
        if grant_first {
            drop(blocker.take());
        }
        // Cancel before the awakened owner polls the newly granted permit.
        drop(receipt);
        while executor.try_tick() {}
        assert_eq!(registry.active(), 0);
        assert_eq!(submitted.get(), 0);
        drop(blocker);
        assert!(stripes.acquire(7).now_or_never().unwrap().is_ok());
        assert!(gate.acquire(Mode::Exclusive).now_or_never().is_some());
        smol::block_on(registry.drain());
    }
}

#[test]
fn cancel_ack_and_partial_submission_do_not_release_original_owners() {
    use futures::channel::oneshot;
    use rublk::raid5::transaction::Registry;
    use std::{cell::Cell, rc::Rc};
    for submitted_count in [1, 2, 3] {
        let executor = smol::LocalExecutor::new();
        let mut registry = Registry::new(1, &executor).unwrap();
        let stripes = StripeWorkTable::new(1);
        let gate = FlushGate::default();
        let (g, t) = (gate.clone(), stripes.clone());
        let (senders, originals): (Vec<_>, Vec<_>) = (0..submitted_count)
            .map(|_| oneshot::channel::<()>())
            .unzip();
        let (ack_sender, ack) = oneshot::channel::<i32>();
        let acknowledged = Rc::new(Cell::new(false));
        let seen = acknowledged.clone();
        let cancel_owner = executor.spawn(async move {
            assert_eq!(ack.await.unwrap(), 0);
            seen.set(true);
        });
        let receipt = registry
            .spawn(0, move |_| async move {
                let _admission = g.acquire(Mode::Shared).await;
                let permit = t
                    .acquire(0)
                    .await
                    .map_err(|_| anyhow::anyhow!("failed stripe"))?;
                // A partial submission failure must still drain every operation
                // already accepted, even if cancellation was acknowledged first.
                for original in originals {
                    original.await?;
                }
                permit.mark_failed();
                Err(anyhow::anyhow!("partial submission failed"))
            })
            .unwrap();
        while executor.try_tick() {}
        let mut successor = Box::pin(stripes.acquire(0));
        let mut flush = Box::pin(gate.acquire(Mode::Exclusive));
        assert!(pending(successor.as_mut()));
        assert!(pending(flush.as_mut()));
        drop(receipt);
        registry.close(true);
        ack_sender.send(0).unwrap();
        while executor.try_tick() {}
        assert!(acknowledged.get());
        for send in senders {
            assert_eq!(registry.active(), 1);
            assert!(pending(successor.as_mut()));
            assert!(pending(flush.as_mut()));
            send.send(()).unwrap();
            while executor.try_tick() {}
        }
        assert_eq!(registry.active(), 0);
        assert!(matches!(
            successor.now_or_never(),
            Some(Err(FailedStripe(0)))
        ));
        assert!(flush.now_or_never().is_some());
        smol::block_on(cancel_owner);
        smol::block_on(registry.drain());
    }
}

#[test]
fn failed_waiters_on_other_threads_submit_nothing_after_entry_reclaim() {
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        mpsc, Arc,
    };
    let stripes = StripeWorkTable::new(1); // Deliberately collide every stripe.
    let owner = stripes.acquire(19).now_or_never().unwrap().unwrap();
    let submitted = Arc::new(AtomicUsize::new(0));
    let (send, receive) = mpsc::channel();
    std::thread::scope(|scope| {
        let mut workers = Vec::new();
        for _ in 0..2 {
            let (t, count, send) = (stripes.clone(), submitted.clone(), send.clone());
            workers.push(scope.spawn(move || {
                let mut queued = Box::pin(t.acquire(19));
                assert!(pending(queued.as_mut()));
                send.send(()).unwrap();
                if smol::block_on(queued).is_ok() {
                    count.fetch_add(1, Ordering::SeqCst);
                }
            }));
        }
        receive.recv().unwrap();
        receive.recv().unwrap();
        owner.mark_failed();
        drop(owner);
        for worker in workers {
            worker.join().unwrap();
        }
    });
    // The transient FIFO entry is gone now. Its failure tombstone must survive.
    assert!(matches!(
        stripes.acquire(19).now_or_never(),
        Some(Err(FailedStripe(19)))
    ));
    assert_eq!(submitted.load(Ordering::SeqCst), 0);
    assert!(stripes.acquire(20).now_or_never().unwrap().is_ok());
}
