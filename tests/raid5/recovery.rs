// SPDX-License-Identifier: GPL-2.0-or-later
use super::*;
#[test]
fn only_contiguous_completed_range_advances() {
    let mut f = Frontier::new(100, 32).unwrap();
    f.complete(2).unwrap();
    f.complete(1).unwrap();
    assert_eq!(f.first_unsynced(), 0);
    f.complete(0).unwrap();
    assert_eq!(f.first_unsynced(), 3);
    assert!(!f.schedulable(35));
    assert!(f.schedulable(34));
}
#[test]
fn far_repair_bounded_and_restart_from_zero() {
    let mut f = Frontier::new(1000000, 32).unwrap();
    for i in 32..1000000 {
        f.complete(i).unwrap();
    }
    assert!(f.completed.is_empty());
    for i in (0..32).rev() {
        f.complete(i).unwrap();
    }
    assert_eq!(f.first_unsynced(), 32);
    assert_eq!(Frontier::new(1000000, 32).unwrap().first_unsynced(), 0);
}
#[test]
fn frontier_never_clears_failed_or_proves_durable_clean() {
    let mut f = Frontier::new(1, 1).unwrap();
    f.complete(0).unwrap();
    assert!(f.done());
    assert!(f.complete(1).is_err());
    assert!(!f.schedulable(0));
}
