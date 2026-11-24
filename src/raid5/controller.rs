// SPDX-License-Identifier: GPL-2.0-or-later
//! Two-phase worker startup and joined teardown. No writable half-device.
use super::{backend::Backends, engine::Shared, queue, sync_worker};
use anyhow::{ensure, Result};
use libublk::{ctrl::UblkCtrl, io::UblkDev};
use std::sync::{
    mpsc::{self, RecvTimeoutError},
    Arc,
};
/// Can only be constructed after every worker joined and original CQEs drained.
pub struct Drained {
    _private: (),
}
enum Event {
    Ready {
        worker: usize,
        tid: i32,
    },
    Done {
        worker: usize,
        error: Option<String>,
    },
}
#[derive(Clone, Copy)]
pub struct Workers {
    pub sync_window: usize,
    pub sync_rate: u64,
}
pub fn run(
    ctrl: &UblkCtrl,
    backends: Arc<Backends>,
    shared: Arc<Shared>,
    config: Workers,
    json: serde_json::Value,
    on_ready: impl FnOnce(&UblkCtrl) -> Result<()>,
) -> Result<Drained> {
    let count = ctrl.dev_info().nr_hw_queues as usize;
    ensure!(count > 0, "no normal queues");
    ensure!(
        ctrl.dev_info().flags & libublk::sys::UBLK_F_AUTO_BUF_REG as u64 != 0,
        "auto registration required"
    );
    ensure!(
        ctrl.dev_info().flags & libublk::sys::UBLK_F_USER_COPY as u64 == 0,
        "user copy forbidden"
    );
    ensure!(
        ctrl.dev_info().flags & libublk::sys::UBLK_F_USER_RECOVERY_REISSUE as u64 == 0,
        "request reissue forbidden"
    );
    let status = super::status::Publisher::new(
        ctrl.dev_info().dev_id,
        serde_json::from_value(json["raid5"]["array_uuid"].clone())?,
        json["raid5"]["generation"]
            .as_u64()
            .ok_or_else(|| anyhow::anyhow!("missing session generation"))?,
    );
    shared.sync_control.rate(config.sync_rate);
    let control = super::control::Server::bind(
        ctrl.dev_info().dev_id,
        serde_json::from_value(json["raid5"]["array_uuid"].clone())?,
        json["raid5"]["generation"].as_u64().unwrap(),
    )?;
    let dev = Arc::new(UblkDev::new(
        "raid5".into(),
        |dev| {
            dev.set_default_params(shared.geometry.capacity());
            dev.tgt.params.basic.logical_bs_shift = backends.sectors[0].trailing_zeros() as u8;
            dev.tgt.params.basic.physical_bs_shift = dev.tgt.params.basic.logical_bs_shift.max(12);
            dev.tgt.params.basic.io_min_shift = dev.tgt.params.basic.logical_bs_shift;
            dev.tgt.params.basic.io_opt_shift = 17;
            for (i, fd) in backends.fds().into_iter().enumerate() {
                dev.tgt.fds[i + 1] = fd;
            }
            dev.tgt.nr_fds = 4;
            dev.set_target_json(json);
            Ok(())
        },
        ctrl,
    )?);
    let (events, rx) = mpsc::channel();
    let mut boot = Vec::new();
    let mut handles = Vec::new();
    for worker in 0..=count {
        let (go, wait) = mpsc::channel::<bool>();
        boot.push(go);
        let events = events.clone();
        let dev = dev.clone();
        let backends = backends.clone();
        let shared = shared.clone();
        handles.push(std::thread::spawn(move || -> Result<()> {
            let tid = UblkCtrl::init_queue_thread();
            let ready = || -> Result<bool> {
                events.send(Event::Ready { worker, tid })?;
                Ok(wait.recv()?)
            };
            let worker_shared = shared.clone();
            let result = if worker < count {
                queue::serve(worker as u16, &dev, &backends, shared, ready)
            } else {
                sync_worker::serve(
                    &backends,
                    shared,
                    config.sync_window,
                    config.sync_rate,
                    ready,
                )
            };
            // Publish before queue peers drain or the controller's START
            // completes. A receipt/event alone cannot unblock pending FETCH.
            if result.is_err() {
                worker_shared
                    .error
                    .store(true, std::sync::atomic::Ordering::Release);
                worker_shared.request_stop();
            }
            let _ = events.send(Event::Done {
                worker,
                error: result.as_ref().err().map(|e| format!("{e:#}")),
            });
            result
        }));
    }
    drop(events);
    let mut readiness = vec![None; count + 1];
    let mut ended = vec![false; count + 1];
    let mut failure = None;
    // No worker has submitted FETCH or backend I/O while this phase waits.
    while readiness.iter().any(Option::is_none) {
        match rx.recv() {
            Ok(Event::Ready { worker, tid }) => readiness[worker] = Some(tid),
            Ok(Event::Done { worker, error }) => {
                ended[worker] = true;
                failure = Some(error.unwrap_or_else(|| "worker exited before activation".into()));
                break;
            }
            Err(e) => {
                failure = Some(e.to_string());
                break;
            }
        }
    }
    if failure.is_none() {
        for (qid, tid) in readiness[..count].iter().enumerate() {
            ctrl.set_thread_affinity(qid as u16, tid.unwrap());
            if let Err(e) = ctrl.configure_queue(&dev, qid as u16, tid.unwrap()) {
                failure = Some(format!("queue configuration: {e}"));
                break;
            }
        }
    }
    if failure.is_none() {
        if let Err(e) = status.publish(&shared, Some("STARTING"), None) {
            failure = Some(format!("status bootstrap: {e}"));
        }
    }
    if failure.is_some() {
        for sender in boot {
            let _ = sender.send(false);
        }
        for handle in handles {
            let _ = handle.join();
        }
        anyhow::bail!(
            "RAID5 bootstrap rolled back before publication: {}",
            failure.unwrap()
        );
    }
    // All queues and the dedicated sync ring have valid objects, maps and file
    // tables. Arm FETCH only now; START waits for the driver's queue readiness.
    shared.start_sync_clock();
    for sender in boot {
        if sender.send(failure.is_none()).is_err() {
            failure.get_or_insert("worker activation channel closed".into());
        }
    }
    let mut started = false;
    if failure.is_none() {
        match ctrl.start_dev_cancellable(&dev, || {
            shared.stop.load(std::sync::atomic::Ordering::Acquire)
                || shared.error.load(std::sync::atomic::Ordering::Acquire)
        }) {
            Ok(n) if n >= 0 => {
                started = true;
                let notification = if shared.stop.load(std::sync::atomic::Ordering::Acquire)
                    || shared.error.load(std::sync::atomic::Ordering::Acquire)
                {
                    Err(anyhow::anyhow!("worker failed while START completed"))
                } else {
                    status
                        .publish(&shared, None, None)
                        .and_then(|_| on_ready(ctrl))
                };
                if let Err(e) = notification {
                    failure = Some(format!("readiness notification: {e}"));
                }
            }
            Ok(n) => failure = Some(format!("START CQE {n}")),
            Err(e) => failure = Some(format!("START: {e}")),
        }
    }
    if failure.is_some() {
        shared.request_stop();
        let _ = ctrl.stop_dev();
    }
    let mut next_status = std::time::Instant::now();
    while ended.iter().any(|done| !*done) {
        match rx.recv_timeout(std::time::Duration::from_millis(50)) {
            Ok(Event::Done { worker, error }) => {
                ended[worker] = true;
                if let Some(error) = error {
                    failure.get_or_insert(error);
                    shared
                        .error
                        .store(true, std::sync::atomic::Ordering::Release);
                    shared.request_stop();
                }
                if worker < count {
                    shared.request_stop();
                }
            }
            Ok(Event::Ready { .. }) => (),
            Err(RecvTimeoutError::Timeout) => (),
            Err(RecvTimeoutError::Disconnected) => break,
        }
        control.poll(&shared);
        if std::time::Instant::now() >= next_status {
            if let Err(e) = status.publish(&shared, None, None) {
                super::log(format_args!("RAID5 status snapshot: {e}"));
            }
            next_status = std::time::Instant::now() + std::time::Duration::from_millis(200);
        }
        if shared.error.load(std::sync::atomic::Ordering::Acquire) {
            failure.get_or_insert("queue/backend/sync failure".into());
        }
        if shared.stop.load(std::sync::atomic::Ordering::Acquire)
            || shared.error.load(std::sync::atomic::Ordering::Acquire)
        {
            shared.request_stop();
            let _ = ctrl.stop_dev();
        }
    }
    for handle in handles {
        match handle.join() {
            Ok(Ok(())) => (),
            Ok(Err(e)) => {
                failure.get_or_insert(format!("{e:#}"));
            }
            Err(_) => {
                failure.get_or_insert("worker panicked".into());
            }
        }
    }
    // Joining includes all thread-local destructors, not merely a reported PID.
    shared.request_stop();
    let _ = ctrl.stop_dev();
    drop(dev);
    if let Some(error) = failure {
        anyhow::bail!("RAID5 stopped with error after drain: {error}")
    }
    ensure!(started, "RAID5 device never started");
    Ok(Drained { _private: () })
}
