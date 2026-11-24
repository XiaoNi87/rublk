// SPDX-License-Identifier: GPL-2.0-or-later
#![cfg(feature = "raid5")]
use libublk::{
    io::{ublk_init_bpf_task_ring, ublk_init_task_ring},
    UblkError,
};
fn errno(result: Result<(), UblkError>, expected: i32) {
    assert!(matches!(result,Err(UblkError::OtherError(n)) if n == -expected));
}
#[test]
fn invalid_ring_geometry_rejected_before_kernel_access() {
    errno(ublk_init_bpf_task_ring(0, 128), libc::EINVAL);
    errno(ublk_init_bpf_task_ring(64, 32), libc::EINVAL);
}
#[test]
#[ignore = "requires root and pinned io_uring-BPF kernel; explicitly run --ignored"]
fn root_repeat_is_idempotent_but_geometry_change_is_rejected() {
    assert_eq!(unsafe { libc::geteuid() }, 0);
    std::thread::spawn(|| {
        ublk_init_bpf_task_ring(64, 128).unwrap();
        ublk_init_bpf_task_ring(64, 128).unwrap();
        errno(ublk_init_bpf_task_ring(128, 256), libc::EEXIST);
    })
    .join()
    .unwrap();
}
#[test]
#[ignore = "requires root and io_uring; explicitly run --ignored"]
fn root_existing_ordinary_ring_cannot_be_silently_reused() {
    assert_eq!(unsafe { libc::geteuid() }, 0);
    std::thread::spawn(|| {
        ublk_init_task_ring(|cell| {
            cell.set(std::cell::RefCell::new(
                io_uring::IoUring::new(64).map_err(UblkError::IOError)?,
            ))
            .map_err(|_| UblkError::OtherError(-libc::EEXIST))
        })
        .unwrap();
        errno(ublk_init_bpf_task_ring(64, 128), libc::EEXIST);
    })
    .join()
    .unwrap();
}
