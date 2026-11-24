// SPDX-License-Identifier: GPL-2.0-or-later
//! Dedicated normal queue ownership: ring/BPF/arena/registry all die here,
//! after original CQEs. Bootstrap callback precedes ANY submitted fetch.
use super::{
    backend::Backends,
    bpf_runtime::BpfRuntime,
    engine::{self, Shared},
    queue_wake::QueueWake,
    transaction::Registry,
};
use anyhow::{ensure, Result};
use io_uring::{opcode, types};
use libublk::{
    io::{UblkDev, UblkQueue},
    BufDesc, UblkError,
};
use std::{
    cell::{Cell, RefCell},
    mem::MaybeUninit,
    os::fd::AsRawFd,
    rc::Rc,
    sync::Arc,
};
/// A delivered request is valid only after the protocol's exact success CQE.
/// Publish fatal errors before this tag leaves the registry: peers may still
/// be waiting for FETCH and cannot finish until the controller stops delivery.
fn command_result(shared: &Shared, phase: &str, result: Result<i32, UblkError>) -> Result<()> {
    let error = match result {
        Ok(n) if n == libublk::sys::UBLK_IO_RES_OK as i32 => return Ok(()),
        Err(UblkError::QueueIsDown) => return Err(UblkError::QueueIsDown.into()),
        Ok(n) => anyhow::anyhow!("{phase}: command CQE {n}, expected UBLK_IO_RES_OK"),
        Err(e) => anyhow::anyhow!(e).context(phase.to_owned()),
    };
    super::log(format_args!("RAID5 fatal delivery command: {error:#}"));
    shared.transaction_error(&error);
    Err(error)
}
pub fn serve(
    qid: u16,
    dev: &UblkDev,
    _backends: &Backends,
    shared: Arc<Shared>,
    ready: impl FnOnce() -> Result<bool>,
) -> Result<()> {
    let _metrics = super::metrics::Scope::new(|| format!("queue-{qid}"));
    let depth = dev.dev_info.queue_depth as usize;
    let entries = (depth
        .checked_mul(16)
        .ok_or_else(|| anyhow::anyhow!("ring size overflow"))?)
    .max(64)
    .next_power_of_two();
    libublk::io::ublk_init_bpf_task_ring(entries as u32, (entries * 2) as u32)?;
    let q = Rc::new(UblkQueue::new(qid, dev)?);
    let fd = libublk::with_task_io_ring(|r| r.as_raw_fd());
    let mut storage = MaybeUninit::uninit();
    let runtime = BpfRuntime::load(&mut storage, fd, depth)?;
    let wake = QueueWake::new()?;
    shared.register_wake(&wake);
    // Failed peer bootstrap returns false: no FETCH/notification/backend SQE exists.
    if !ready()? {
        return Ok(());
    }
    let executor = smol::LocalExecutor::new();
    let registry = RefCell::new(Registry::new(depth, &executor)?);
    let mut receipts = Vec::new();
    for index in 0..depth {
        let q = q.clone();
        let runtime = &runtime;
        let wake = wake.clone();
        let shared = shared.clone();
        let max_bytes = dev.dev_info.max_io_buf_bytes;
        receipts.push(
            registry
                .borrow_mut()
                .spawn(index as u16, move |cancel| async move {
                    let tag = index as u16;
                    let descriptor = BufDesc::AutoReg(libublk::sys::ublk_auto_buf_reg {
                        index: tag,
                        flags: 0,
                        ..Default::default()
                    });
                    command_result(
                        &shared,
                        "FETCH",
                        q.submit_io_prep_cmd(tag, descriptor.clone(), 0, None).await,
                    )?;
                    loop {
                        if shared.stop.load(std::sync::atomic::Ordering::Acquire) {
                            q.fail_io_cmd(tag, descriptor.clone()).await?;
                            return Ok(0);
                        }
                        let iod = *q.get_iod(tag);
                        // SAFETY: queue registry owns this whole tag loop. Neither caller/result
                        // drop nor shutdown aborts it; every backend/BPF future is awaited to CQE.
                        let timer = super::metrics::Timer::start();
                        let outcome = unsafe {
                            engine::request(
                                &shared,
                                runtime,
                                &wake,
                                cancel.clone(),
                                tag,
                                iod,
                                [1, 2, 3],
                                max_bytes,
                            )
                            .await
                        };
                        timer.record(super::metrics::Kind::Request);
                        let result = match outcome {
                            Ok(n) => n,
                            Err(e) => {
                                super::log(format_args!("RAID5 q={qid} tag={tag}: {e:#}"));
                                // Only typed pre-I/O cancellation is benign. A real
                                // read/BPF error racing stop must still forbid CLEAN.
                                shared.transaction_error(&e);
                                -libc::EIO
                            }
                        };
                        if q.is_stopping() || shared.stop.load(std::sync::atomic::Ordering::Acquire)
                        {
                            q.fail_io_cmd(tag, descriptor.clone()).await?;
                            return Ok(0);
                        }
                        let completion = q
                            .submit_io_commit_cmd(tag, descriptor.clone(), result)
                            .await;
                        if let Err(error) = command_result(&shared, "COMMIT_AND_FETCH", completion)
                        {
                            if !error
                                .downcast_ref::<UblkError>()
                                .is_some_and(|e| matches!(e, UblkError::QueueIsDown))
                            {
                                // The rejected command may leave the client request
                                // owned by this task. Complete it with EIO, without
                                // executing its data operation a second time.
                                q.fail_io_cmd(tag, descriptor.clone()).await?;
                            }
                            return Err(error);
                        }
                    }
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
                ensure!(result >= 0, "queue notification CQE {result}");
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
    // STOP can wait for a request whose rejected COMMIT never reached the
    // driver. Cancel only pending delivery commands (fixed file 0), never
    // backend/BPF operations, and let every independent owner drain naturally.
    let mut cancellation = None;
    loop {
        while executor.try_tick() {}
        if q.is_stopping() {
            shared.request_stop();
        }
        if shared.stop.load(std::sync::atomic::Ordering::Acquire) {
            registry.borrow_mut().close(true);
            if cancellation.is_none() {
                cancellation = Some(executor.spawn(async {
                    libublk::uring_async::ublk_submit_sqe_async(
                        opcode::AsyncCancel2::new(types::CancelBuilder::fd(types::Fixed(0)).all())
                            .build(),
                        libublk::UblkUringData::Target as u64,
                    )
                    .await
                }));
            }
        }
        if registry.borrow().active() == 0 && !notify_stop.replace(true) {
            wake.signal();
        }
        while executor.try_tick() {}
        if registry.borrow().active() == 0
            && notification.is_finished()
            && cancellation.as_ref().is_none_or(|t| t.is_finished())
        {
            break;
        }
        let timeout = types::Timespec::new().sec(1);
        let timed_out =
            libublk::uring_async::uring_poll_io_fn::<io_uring::squeue::Entry>(&q, Some(timeout), 1);
        libublk::uring_async::ublk_reap_io_events_with_update_queue(
            &q,
            matches!(timed_out, Ok(true)),
            None,
            |cqe| libublk::uring_async::ublk_wake_task(cqe.user_data(), cqe),
        )?;
    }
    if let Some(task) = cancellation {
        let result = smol::block_on(task)?;
        ensure!(
            result >= 0 || result == -libc::ENOENT,
            "delivery cancellation CQE {result}"
        );
    }
    // Explicitly consume original notification completion before its fd disappears.
    let notification_result = smol::block_on(notification);
    let mut failure = None;
    for receipt in receipts {
        match smol::block_on(receipt) {
            Ok(_) => (),
            Err(e)
                if e.downcast_ref::<UblkError>()
                    .is_some_and(|e| matches!(e, UblkError::QueueIsDown)) =>
            {
                ()
            }
            Err(e) => {
                failure.get_or_insert(e);
            }
        }
    }
    smol::block_on(registry.borrow_mut().drain());
    ensure!(
        registry.borrow().active() == 0,
        "queue teardown with live transactions"
    );
    notification_result?;
    if let Some(error) = failure {
        return Err(error);
    }
    Ok(())
}
