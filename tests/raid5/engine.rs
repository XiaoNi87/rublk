// SPDX-License-Identifier: GPL-2.0-or-later
use super::*;
#[test]
fn shutdown_does_not_reclassify_real_cqe_error_as_cancellation() {
    let geometry = Geometry::raid5([super::super::geometry::PREFIX + 3 * CHUNK; 3]).unwrap();
    let shared = Shared::new(geometry, 64, 2, true).unwrap();
    shared.request_stop();
    assert!(!shared.transaction_error(&super::super::transaction::BeforeIoCancelled.into()));
    assert!(!shared.error.load(Ordering::Acquire));
    assert!(shared.transaction_error(&anyhow::anyhow!("data read: CQE -5")));
    assert!(shared.error.load(Ordering::Acquire));
}
#[test]
fn complete_frontier_is_not_finalized_recovery() {
    let geometry = Geometry::raid5([super::super::geometry::PREFIX + 3 * CHUNK; 3]).unwrap();
    let shared = Shared::new(geometry, 64, 2, false).unwrap();
    shared.start_sync_clock();
    for stripe in 0..3 {
        shared.complete_stripe(stripe).unwrap();
    }
    assert!(shared.scan_done());
    assert!(!shared.sync_done());
    assert!(shared.sync_scan_completed_ns.load(Ordering::Acquire) > 0);
    assert_eq!(shared.sync_completed_ns.load(Ordering::Acquire), 0);
    assert_eq!(
        shared.sync_finalization_started_ns.load(Ordering::Acquire),
        0
    );
}
