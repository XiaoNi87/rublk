// SPDX-License-Identifier: GPL-2.0-or-later
//! Independent BPF/ring/arena, bounded window and shared stripe/flush protocol.
use super::{
    backend::Backends,
    bpf_runtime::BpfRuntime,
    engine::{self, Shared},
    geometry::CHUNK,
    queue_wake::QueueWake,
    transaction::Registry,
};
use anyhow::{ensure, Result};
use io_uring::{opcode, types};
use std::{
    cell::{Cell, RefCell},
    mem::MaybeUninit,
    os::fd::AsRawFd,
    rc::Rc,
    sync::{atomic::Ordering, Arc},
    time::{Duration, Instant},
};
pub fn serve(
    backends: &Backends,
    shared: Arc<Shared>,
    window: usize,
    _rate: u64,
    ready: impl FnOnce() -> Result<bool>,
) -> Result<()> {
    let _metrics = super::metrics::Scope::new(|| "sync".to_string());
    ensure!(window > 0 && window <= 256, "invalid sync window");
    let entries = (window * 16).max(64).next_power_of_two();
    libublk::io::ublk_init_bpf_task_ring(entries as u32, (entries * 2) as u32)?;
    libublk::with_task_io_ring(|r| r.submitter().register_files(&backends.fds()))?;
    let fd = libublk::with_task_io_ring(|r| r.as_raw_fd());
    let mut storage = MaybeUninit::uninit();
    let runtime = BpfRuntime::load(&mut storage, fd, window)?;
    let wake = QueueWake::new()?;
    shared.register_wake(&wake);
    if !ready()? {
        libublk::with_task_io_ring(|r| r.submitter().unregister_files())?;
        return Ok(());
    }
    let executor = smol::LocalExecutor::new();
    let registry = RefCell::new(Registry::new(window + 1, &executor)?);
    let next = Rc::new(Cell::new(0u64));
    let due = Rc::new(Cell::new(Instant::now()));
    let schedule_epoch = Rc::new(Cell::new(0u64));
    let mut receipts = Vec::new();
    for index in 0..window {
        let runtime = &runtime;
        let next = next.clone();
        let due = due.clone();
        let schedule_epoch = schedule_epoch.clone();
        let wake = wake.clone();
        let shared = shared.clone();
        receipts.push(
            registry
                .borrow_mut()
                .spawn(index as u16, move |cancel| async move {
                    loop {
                        if shared.stop.load(Ordering::Acquire)
                            || shared.error.load(Ordering::Acquire)
                            || cancel.requested()
                        {
                            break;
                        }
                        let frontier = shared.first_unsynced();
                        let stripe = next.get().max(frontier);
                        if stripe >= shared.geometry.stripes() {
                            break;
                        }
                        if stripe - frontier >= window as u64 {
                            if cancel
                                .before_io(&wake, smol::Timer::after(Duration::from_millis(1)))
                                .await
                                .is_err()
                            {
                                break;
                            }
                            continue;
                        }
                        next.set(stripe + 1);
                        // Rate and pause waits hold no admission/stripe ownership.
                        let mut reserved: Option<(u64, Instant)> = None;
                        loop {
                            let ctl = shared.sync_control.snapshot();
                            if schedule_epoch.get() != ctl.epoch {
                                due.set(Instant::now());
                                schedule_epoch.set(ctl.epoch);
                            }
                            if ctl.requested {
                                if cancel
                                    .before_io(&wake, smol::Timer::after(Duration::from_millis(10)))
                                    .await
                                    .is_err()
                                {
                                    break;
                                }
                                reserved = None;
                                continue;
                            }
                            let (epoch, start) = match reserved {
                                Some(v) if v.0 == ctl.epoch => v,
                                _ => {
                                    let now = Instant::now();
                                    let start = if ctl.rate == 0 {
                                        now
                                    } else {
                                        due.get().max(now)
                                    };
                                    if ctl.rate > 0 {
                                        due.set(
                                            start
                                                + Duration::from_secs_f64(
                                                    (3 * CHUNK) as f64 / ctl.rate as f64,
                                                ),
                                        );
                                    }
                                    let v = (ctl.epoch, start);
                                    reserved = Some(v);
                                    v
                                }
                            };
                            let now = Instant::now();
                            if start > now {
                                if cancel
                                    .before_io(
                                        &wake,
                                        smol::Timer::at(start.min(now + Duration::from_millis(10))),
                                    )
                                    .await
                                    .is_err()
                                {
                                    break;
                                }
                                continue;
                            }
                            if epoch == shared.sync_control.snapshot().epoch {
                                break;
                            }
                        }
                        if cancel.requested() {
                            break;
                        }
                        // SAFETY: independently owned queue registry retains this future/runtime;
                        // no operation is dropped before its original CQE. All inputs are USER.
                        let result = loop {
                            let result = unsafe {
                                engine::sync_stripe(
                                    &shared,
                                    runtime,
                                    &wake,
                                    cancel.clone(),
                                    index,
                                    stripe,
                                    [0, 1, 2],
                                )
                                .await
                            };
                            if matches!(result, Ok(false)) {
                                if cancel
                                    .before_io(&wake, smol::Timer::after(Duration::from_millis(10)))
                                    .await
                                    .is_err()
                                {
                                    break Ok(false);
                                }
                                continue;
                            }
                            break result;
                        };
                        if let Err(e) = result {
                            if shared.transaction_error(&e) {
                                return Err(e);
                            }
                            break;
                        }
                    }
                    Ok(0)
                })?,
        );
    }
    let notify_stop = Rc::new(Cell::new(false));
    let poll_stop = notify_stop.clone();
    let poll_wake = wake.clone();
    let notification_shared = shared.clone();
    let notification = executor.spawn(async move {
        let result = async {
            loop {
                let result = libublk::uring_async::ublk_submit_sqe_async(
                    opcode::PollAdd::new(types::Fd(poll_wake.fd()), libc::POLLIN as u32).build(),
                    libublk::UblkUringData::Target as u64,
                )
                .await?;
                ensure!(result >= 0, "sync notification CQE {result}");
                poll_wake.drain()?;
                if poll_stop.get() {
                    break;
                }
            }
            Ok::<(), anyhow::Error>(())
        }
        .await;
        if let Err(error) = &result {
            // Peers may wait forever for FETCH or a paused scanner. Publish
            // before this task finishes; the owner keeps driving original CQEs.
            super::log(format_args!("RAID5 fatal notification: {error:#}"));
            notification_shared.transaction_error(error);
        }
        result
    });
    let mut finalization_launched = false;
    loop {
        while executor.try_tick() {}
        if shared.stop.load(Ordering::Acquire) || shared.error.load(Ordering::Acquire) {
            registry.borrow_mut().close(true);
        }
        if registry.borrow().active() == 0
            && !finalization_launched
            && shared.scan_done()
            && !shared.sync_done()
            && !shared.stop.load(Ordering::Acquire)
            && !shared.error.load(Ordering::Acquire)
        {
            finalization_launched = true;
            let shared = shared.clone();
            let wake = wake.clone();
            receipts.push(registry.borrow_mut().spawn(
                window as u16,
                move |cancel| async move {
                    // SAFETY: finalizer is independently registry-owned, with notification
                    // polling alive until its exclusive admission and every fsync drain.
                    let result = unsafe {
                        engine::finalize_sync(&shared, &wake, cancel.clone(), [0, 1, 2]).await
                    };
                    match result {
                        Ok(()) => Ok(0),
                        Err(e) => {
                            if shared.transaction_error(&e) {
                                Err(e)
                            } else {
                                Ok(0)
                            }
                        }
                    }
                },
            )?);
        }
        if registry.borrow().active() == 0 && !notify_stop.replace(true) {
            wake.signal();
        }
        while executor.try_tick() {}
        if registry.borrow().active() == 0 && notification.is_finished() {
            break;
        }
        libublk::with_task_io_ring_mut(|ring| {
            let timeout = types::Timespec::new().sec(1);
            let args = types::SubmitArgs::new().timespec(&timeout);
            match ring.submitter().submit_with_args(1, &args) {
                Ok(_) => (),
                Err(e) if matches!(e.raw_os_error(), Some(libc::EINTR) | Some(libc::ETIME)) => (),
                Err(e) => {
                    super::log(format_args!(
                        "fatal: sync ring wait failed before CQE drain: {e}"
                    ));
                    std::process::abort();
                }
            }
            if let Err(e) = libublk::uring_async::ublk_reap_events_with_handler(ring, |c| {
                libublk::uring_async::ublk_wake_task(c.user_data(), c)
            }) {
                super::log(format_args!("fatal: sync CQE routing: {e}"));
                std::process::abort();
            }
        });
    }
    let notification_result = smol::block_on(notification);
    let mut error = None;
    for receipt in receipts {
        if let Err(e) = smol::block_on(receipt) {
            error = Some(e);
        }
    }
    smol::block_on(registry.borrow_mut().drain());
    libublk::with_task_io_ring(|r| r.submitter().unregister_files())?;
    notification_result?;
    if let Some(e) = error {
        return Err(e);
    }
    Ok(())
}
