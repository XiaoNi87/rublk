// SPDX-License-Identifier: GPL-2.0-or-later
use super::*;
#[test]
fn rotations_and_tail() {
    let g = Geometry::raid5([PREFIX + 3 * CHUNK + 4096; 3]).unwrap();
    assert_eq!(g.capacity(), 6 * CHUNK);
    let s = g.split(0, g.capacity()).unwrap();
    let expected = [(0, 2), (1, 2), (2, 1), (0, 1), (1, 0), (2, 0)];
    for (i, x) in s.iter().enumerate() {
        assert_eq!((x.data_disk, x.parity_disk), expected[i]);
        assert_eq!(x.backend_offset, PREFIX + (i as u64 / 2) * CHUNK);
    }
    assert_eq!(
        g.split(g.capacity() - 512, 512).unwrap()[0].backend_offset,
        PREFIX + 3 * CHUNK - 512
    );
    assert!(g.split(g.capacity(), 512).is_err());
}
#[test]
fn generated_mapping_independent_oracle() {
    let g = Geometry::raid5([PREFIX + 257 * CHUNK; 3]).unwrap();
    let mut seed = 0x5a123456u64;
    for _ in 0..10000 {
        seed = seed.wrapping_mul(6364136223846793005).wrapping_add(1);
        let off = (seed % (g.capacity() / 512)) * 512;
        let len = ((seed >> 32) % 512 + 1) * 512;
        let len = len.min(g.capacity() - off);
        let parts = g.split(off, len).unwrap();
        let mut done = 0;
        for p in parts {
            assert_eq!(p.request_offset, done);
            assert!(p.len > 0 && p.len <= CHUNK);
            assert_ne!(p.data_disk, p.parity_disk);
            let logical = off + done;
            let row = logical / (2 * CHUNK);
            let parity = [2, 1, 0][row as usize % 3];
            let order = (logical / CHUNK) % 2;
            let mut disks = Vec::new();
            for d in 1..=3 {
                let disk = (parity + d) % 3;
                if disk != parity {
                    disks.push(disk);
                }
            }
            assert_eq!(p.data_disk, disks[order as usize]);
            assert_eq!(p.parity_disk, parity);
            assert_eq!(p.backend_offset, PREFIX + row * CHUNK + logical % CHUNK);
            assert!(p.backend_offset >= PREFIX && p.backend_offset + p.len <= PREFIX + 257 * CHUNK);
            done += p.len;
        }
        assert_eq!(done, len);
    }
}
#[test]
fn invalid_and_overflow() {
    assert!(Geometry::new(2, PREFIX + CHUNK).is_err());
    assert!(Geometry::new(3, PREFIX).is_err());
    assert!(Geometry::new(3, 0).is_err());
    assert!(Geometry::new(u64::MAX, PREFIX + CHUNK).is_err());
    assert!(Geometry::new(3, u64::MAX - 4095).is_err());
    assert!(Geometry::raid5([PREFIX + CHUNK, PREFIX + 2 * CHUNK, PREFIX + CHUNK]).is_err());
    let g = Geometry::new(3, PREFIX + CHUNK).unwrap();
    for (off, len) in [(0, 0), (1, 512), (0, 513), (u64::MAX - 511, 1024)] {
        assert!(g.split(off, len).is_err());
    }
}
