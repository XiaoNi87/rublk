// SPDX-License-Identifier: GPL-2.0-or-later
//! Cross-thread notification only; never a data lock. Queue owns a PollAdd SQE.
use std::{
    future::Future,
    io,
    os::fd::{AsRawFd, FromRawFd, OwnedFd},
    sync::Arc,
    task::{Context, Waker},
};
#[derive(Clone)]
pub struct QueueWake(Arc<OwnedFd>);
pub(crate) struct WeakQueueWake(std::sync::Weak<OwnedFd>);
impl WeakQueueWake {
    pub(crate) fn upgrade(&self) -> Option<QueueWake> {
        self.0.upgrade().map(QueueWake)
    }
}
impl QueueWake {
    pub(crate) fn downgrade(&self) -> WeakQueueWake {
        WeakQueueWake(Arc::downgrade(&self.0))
    }
    pub fn new() -> io::Result<Self> {
        let fd = unsafe { libc::eventfd(0, libc::EFD_CLOEXEC | libc::EFD_NONBLOCK) };
        if fd < 0 {
            return Err(io::Error::last_os_error());
        }
        Ok(Self(Arc::new(unsafe { OwnedFd::from_raw_fd(fd) })))
    }
    pub fn fd(&self) -> i32 {
        self.0.as_raw_fd()
    }
    pub fn signal(&self) {
        let value = 1u64;
        loop {
            let n = unsafe { libc::write(self.fd(), &value as *const u64 as *const _, 8) };
            if n == 8 {
                return;
            }
            match io::Error::last_os_error().raw_os_error() {
                Some(libc::EINTR) => continue,
                Some(libc::EAGAIN) => return,
                _ => panic!("eventfd notification failed"),
            }
        }
    }
    pub fn drain(&self) -> io::Result<()> {
        let mut value = 0u64;
        loop {
            let n = unsafe { libc::read(self.fd(), &mut value as *mut u64 as *mut _, 8) };
            if n == 8 {
                continue;
            }
            let e = io::Error::last_os_error();
            match e.raw_os_error() {
                Some(libc::EINTR) => continue,
                Some(libc::EAGAIN) => return Ok(()),
                _ => return Err(e),
            }
        }
    }
    pub async fn wait<F: Future>(&self, future: F) -> F::Output {
        futures::pin_mut!(future);
        futures::future::poll_fn(|cx| {
            let w = futures::task::waker(Arc::new(RingWaker {
                task: cx.waker().clone(),
                queue: self.clone(),
            }));
            future.as_mut().poll(&mut Context::from_waker(&w))
        })
        .await
    }
}
struct RingWaker {
    task: Waker,
    queue: QueueWake,
}
impl futures::task::ArcWake for RingWaker {
    fn wake_by_ref(a: &Arc<Self>) {
        a.task.wake_by_ref();
        a.queue.signal();
    }
}
#[cfg(test)]
#[path = "../../tests/raid5/queue_wake.rs"]
mod tests;
