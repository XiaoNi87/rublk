// SPDX-License-Identifier: GPL-2.0-or-later
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
