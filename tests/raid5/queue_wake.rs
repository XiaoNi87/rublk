// SPDX-License-Identifier: GPL-2.0-or-later
use super::*;
use futures::FutureExt;
#[test]
fn signal_coalesces_and_pollable() {
    let q = QueueWake::new().unwrap();
    for _ in 0..10000 {
        q.signal();
    }
    let mut fd = libc::pollfd {
        fd: q.fd(),
        events: libc::POLLIN,
        revents: 0,
    };
    assert_eq!(unsafe { libc::poll(&mut fd, 1, 0) }, 1);
    q.drain().unwrap();
    assert_eq!(unsafe { libc::poll(&mut fd, 1, 0) }, 0);
    assert_eq!(q.wait(async { 42 }).now_or_never(), Some(42));
}
