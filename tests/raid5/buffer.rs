// SPDX-License-Identifier: GPL-2.0-or-later
use super::*;
#[test]
fn all_slots_are_disjoint_and_fit_exact_allocation() {
    for depth in [1, 8, 32, 128, 256] {
        let b = BufferBudget::new(4, depth, 32, 1024 * 1024 * 1024).unwrap();
        let mut ends = Vec::new();
        for tag in 0..depth {
            let s = b.normal_slots(tag).unwrap();
            for start in [s.old_data, s.old_parity, s.new_parity] {
                ends.push((start, start + CHUNK));
            }
        }
        ends.sort_unstable();
        assert_eq!(ends.first().unwrap().0, 0);
        assert!(ends.windows(2).all(|w| w[0].1 == w[1].0));
        assert_eq!(ends.last().unwrap().1, b.normal_ring_bytes);
        assert!(b.normal_slots(depth).is_err());
        assert_eq!(
            b.sync_slots(31).unwrap().new_parity + CHUNK,
            b.sync_ring_bytes
        );
        assert!(b.sync_slots(32).is_err());
    }
}
#[test]
fn budgets_and_overflow_are_rejected_before_allocation() {
    let b = BufferBudget::new(4, 128, 32, 1 << 30).unwrap();
    assert_eq!(b.arena_bytes, 106954752);
    assert_eq!(b.kernel_request_bytes, 67108864);
    assert_eq!(b.descriptor_bytes, 34816);
    assert!(BufferBudget::new(0, 1, 1, usize::MAX).is_err());
    assert!(BufferBudget::new(1, 0, 1, usize::MAX).is_err());
    assert!(BufferBudget::new(1, 1, 0, usize::MAX).is_err());
    assert!(BufferBudget::new(usize::MAX, 1, 1, usize::MAX).is_err());
    assert!(BufferBudget::new(1, usize::MAX, 1, usize::MAX).is_err());
    assert!(BufferBudget::new(1, 342, 1, usize::MAX).is_err());
    assert!(BufferBudget::new(4, 128, 32, b.arena_bytes + b.descriptor_bytes - 1).is_err());
}
