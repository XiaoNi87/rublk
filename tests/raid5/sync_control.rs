// SPDX-License-Identifier: GPL-2.0-or-later
use super::*;
#[test]
fn pause_ack_follows_drain_and_prevents_new_admission() {
    let c = SyncControl::default();
    let a = c.begin().unwrap();
    c.pause(true);
    assert!(!c.snapshot().paused);
    assert!(c.begin().is_none());
    drop(a);
    assert!(c.snapshot().paused);
    c.pause(false);
    assert!(c.begin().is_some());
    c.rate(123);
    assert_eq!(c.snapshot().rate, 123);
}
