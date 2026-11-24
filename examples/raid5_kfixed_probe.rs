// SPDX-License-Identifier: GPL-2.0-or-later
//! Disposable one-member capability spike, NOT a RAID5 target.
use anyhow::{ensure, Result};
use io_uring::{opcode, types};
use libbpf_rs::{
    skel::{OpenSkel, SkelBuilder},
    MapCore, MapFlags,
};
use libublk::{
    ctrl::{UblkCtrl, UblkCtrlBuilder},
    io::{UblkDev, UblkQueue},
    BufDesc, UblkError, UblkFlags,
};
use std::{
    fs::OpenOptions,
    mem::MaybeUninit,
    os::{fd::AsRawFd, unix::fs::OpenOptionsExt},
    rc::Rc,
};
mod skeleton {
    include!(concat!(env!("OUT_DIR"), "/kfixed.skel.rs"));
}
mod xor_skeleton {
    include!(concat!(env!("OUT_DIR"), "/xor.skel.rs"));
}
#[repr(C)]
#[derive(Default)]
struct XorDescriptor {
    nr: u32,
    len: u32,
    output: u32,
    reserved: u32,
    source: [io_uring::bpf::BufferDescriptor; 3],
}
fn request_byte(i: usize, key: u8) -> u8 {
    ((i * 13 + 17) ^ ((i >> 8) * 29) ^ ((i >> 12) * 71)) as u8 ^ key
}
#[allow(dead_code)]
#[path = "raid5_xor_probe.rs"]
mod sync_probe;
static NORMAL_IO_COUNT: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
fn coordination() -> &'static (
    rublk::raid5::flush_gate::FlushGate,
    rublk::raid5::stripe::StripeWorkTable,
) {
    static STATE: std::sync::OnceLock<(
        rublk::raid5::flush_gate::FlushGate,
        rublk::raid5::stripe::StripeWorkTable,
    )> = std::sync::OnceLock::new();
    STATE.get_or_init(|| {
        (
            Default::default(),
            rublk::raid5::stripe::StripeWorkTable::new(64),
        )
    })
}
fn queue(qid: u16, dev: &UblkDev) -> Result<()> {
    libublk::io::ublk_init_bpf_task_ring(64, 128)?;
    let q = Rc::new(UblkQueue::new(qid, dev)?);
    let fd = libublk::with_task_io_ring(|r| r.as_raw_fd());
    let mut obj = MaybeUninit::uninit();
    let open = skeleton::KfixedSkelBuilder::default().open(&mut obj)?;
    // SAFETY: generated skeleton's mutable initial struct_ops storage, before load.
    unsafe {
        (*open.struct_ops.kfixed_ops).ring_fd = fd;
    }
    let mut skel = open.load()?;
    let _link = skel.maps.kfixed_ops.attach_struct_ops()?;
    let mut xor_obj = MaybeUninit::uninit();
    let xor_open = xor_skeleton::XorSkelBuilder::default().open(&mut xor_obj)?;
    unsafe {
        (*xor_open.struct_ops.xor_ops).ring_fd =
            if qid == 1 && std::env::var_os("RAID5_SPIKE_FAIL_SECOND_ATTACH").is_some() {
                -1
            } else {
                fd
            };
        (*xor_open.struct_ops.xor_ops).id = 1;
    }
    let mut xor_skel = xor_open.load()?;
    use libbpf_rs::AsRawLibbpf;
    let mut ignored = 0;
    let arena = unsafe {
        libbpf_rs::libbpf_sys::bpf_map__initial_value(
            xor_skel.maps.arena.as_libbpf_object().as_ptr(),
            &mut ignored,
        )
    } as usize;
    ensure!(arena != 0, "arena mapping absent");
    unsafe {
        std::ptr::write_bytes(arena as *mut u8, 0, 196608);
    }
    xor_skel.maps.bss_data.as_mut().unwrap().arena_base = arena as *mut u8;
    let _xor_link = xor_skel.maps.xor_ops.attach_struct_ops()?;
    eprintln!("QUEUE qid={qid} ring_fd={fd} arena={arena:#x} arena_map={} kfixed_table_index=0 backing_fixed_fd=1",xor_skel.maps.arena.info()?.info.id);
    let source_file = tempfile::NamedTempFile::new()?;
    use std::os::unix::fs::FileExt;
    source_file.as_file().set_len(131072)?;
    source_file.as_file().write_all_at(&vec![0x3c; 65536], 0)?;
    source_file
        .as_file()
        .write_all_at(&vec![0xa7; 65536], 65536)?;
    let source_direct = OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECT)
        .open(source_file.path())?;
    let parity_file = tempfile::NamedTempFile::new()?;
    parity_file.as_file().set_len(65536)?;
    let parity_direct = OpenOptions::new()
        .write(true)
        .custom_flags(libc::O_DIRECT)
        .open(parity_file.path())?;
    let exe = smol::LocalExecutor::new();
    let q1 = q.clone();
    let wake = rublk::raid5::queue_wake::QueueWake::new()?;
    let wake_for_task = wake.clone();
    let wake_for_poll = wake.clone();
    let wake_stop = Rc::new(std::cell::Cell::new(false));
    let poll_stop = wake_stop.clone();
    let poll_q = q.clone();
    let wake_task = exe.spawn(async move {
        loop {
            let n = poll_q
                .ublk_submit_sqe(
                    opcode::PollAdd::new(types::Fd(wake_for_poll.fd()), libc::POLLIN as u32)
                        .build(),
                )
                .await;
            ensure!(n >= 0, "eventfd poll CQE {n}");
            wake_for_poll.drain()?;
            if poll_stop.get() {
                break;
            }
        }
        Ok::<(), anyhow::Error>(())
    });
    let occupied = libublk::helpers::IoBuf::<u8>::new(4096);
    let release_path = std::env::var_os("RAID5_SPIKE_RELEASE_FILE").map(std::path::PathBuf::from);
    if release_path.is_some() {
        let iov = libc::iovec {
            iov_base: occupied.as_ptr() as *mut _,
            iov_len: 4096,
        };
        // SAFETY: occupied remains allocated until explicit table removal below.
        libublk::with_task_io_ring(|r| unsafe {
            r.submitter().register_buffers_update(0, &[iov], None)
        })?;
        eprintln!("INJECT: sparse buffer slot 0 occupied; auto-reg must fail -EBUSY");
    }
    let clear = exe.spawn(async move {
        if let Some(path) = release_path {
            while !path.exists() {
                smol::Timer::after(std::time::Duration::from_millis(20)).await;
            }
            let iov = libc::iovec {
                iov_base: std::ptr::null_mut(),
                iov_len: 0,
            };
            // SAFETY: empty entry removes the occupied slot; no I/O uses it.
            libublk::with_task_io_ring(|r| unsafe {
                r.submitter().register_buffers_update(0, &[iov], None)
            })?;
            eprintln!("INJECT: sparse slot 0 cleared; outstanding fetch must remain usable");
        }
        Ok::<_, anyhow::Error>(())
    });
    let pending_spec = io_uring::types::Timespec::new().sec(300);
    let pending_future = q.ublk_submit_sqe(opcode::Timeout::new(&pending_spec).build());
    let pending_token = pending_future.user_data;
    let pending = exe.spawn(async move { pending_future.await });
    let cancel = std::cell::RefCell::new(None::<smol::Task<i32>>);
    let output = &skel.maps.output;
    let task = exe.spawn(async move {
        let desc = BufDesc::AutoReg(libublk::sys::ublk_auto_buf_reg {
            index: 0,
            flags: 0,
            ..Default::default()
        });
        q1.submit_io_prep_cmd(0, desc.clone(), 0, None).await?;
        loop {
            let request_result: Result<i32> = async {
            let iod = q1.get_iod(0);
            let op = iod.op_flags & 255;
            let bytes = iod.nr_sectors << 9;
            let off = iod.start_sector << 9;
                let key=(iod.start_sector%251) as u8;
            if std::env::var("RAID5_SPIKE_HOLD_OFFSET").ok().and_then(|v|v.parse::<u64>().ok())==Some(off) {
                eprintln!("HOLD fetched qid={qid} tag=0 offset={off}");
                futures::future::pending::<()>().await;
            }
            let (gate,stripes)=coordination();
            let _admission=if op==libublk::sys::UBLK_IO_OP_FLUSH {
                Some(wake_for_task.wait(gate.acquire(rublk::raid5::flush_gate::Mode::Exclusive)).await)
            }else if op==libublk::sys::UBLK_IO_OP_WRITE {
                Some(wake_for_task.wait(gate.acquire(rublk::raid5::flush_gate::Mode::Shared)).await)
            }else{None};
            let _stripe=if op!=libublk::sys::UBLK_IO_OP_FLUSH {
                Some(wake_for_task.wait(stripes.acquire(off/131072)).await.map_err(|e|anyhow::anyhow!("failed stripe {}",e.0))?)
            }else{None};
            let mut result = bytes as i32;
            if op == libublk::sys::UBLK_IO_OP_WRITE && bytes >= 8192 {
                let b = io_uring::bpf::BufferDescriptor {
                    kind: io_uring::bpf::KFIXED,
                    len: 64,
                    addr: 4096,
                    ..Default::default()
                };
                let cqe = q1
                    .ublk_submit_sqe(opcode::Bpf::new((&b as *const _) as *const u8, 1).build())
                    .await;
                if cqe != 64 {
                    return Err(anyhow::anyhow!("BPF CQE {cqe}"));
                }
                let got = output.lookup(&0u32.to_ne_bytes(), MapFlags::ANY)?.unwrap();
                ensure!(
                    got == (4096..4160).map(|i|request_byte(i,key)).collect::<Vec<_>>(),
                    "KFIXED offset bytes mismatch"
                );
                eprintln!("PASS: qid={qid} tag=0 KFIXED BPF offset 4096 exact CQE/output 64 key={key}");
                    let probe_len=(bytes-4096).min(65536);
                for (slot, off) in [(0usize, 0u64), (65536, 65536)] {
                    let n = q1.ublk_submit_sqe(opcode::Read::new(types::Fd(source_direct.as_raw_fd()), (arena+slot) as *mut u8, probe_len).offset(off).build()).await;
                    ensure!(n == probe_len as i32, "arena old-data/parity Read CQE {n}");
                }
                let u = |offset| io_uring::bpf::BufferDescriptor {len:probe_len, addr:(arena+offset) as u64, ..Default::default()};
                let k = io_uring::bpf::BufferDescriptor {kind:io_uring::bpf::KFIXED, len:probe_len, addr:4096, ..Default::default()};
                let d = XorDescriptor {nr:3,len:probe_len,output:131072,reserved:0,source:[u(0),u(65536),k]};
                let n = q1.ublk_submit_sqe(opcode::Bpf::new((&d as *const XorDescriptor).cast(),1).operation(1).build()).await;
                ensure!(n == probe_len as i32,"mixed RMW BPF CQE {n}");
                let got = unsafe {std::slice::from_raw_parts((arena+131072) as *const u8, probe_len as usize)};
                let expected = (0..probe_len as usize).map(|i|request_byte(4096+i,key)^0x3c^0xa7).collect::<Vec<_>>();
                ensure!(got == expected, "mixed USER/USER/KFIXED parity mismatch");
                let n = q1.ublk_submit_sqe(opcode::Write::new(types::Fd(parity_direct.as_raw_fd()),got.as_ptr(),probe_len).build()).await;
                ensure!(n == probe_len as i32, "arena parity Write CQE {n}");
                ensure!(q1.ublk_submit_sqe(opcode::Fsync::new(types::Fd(parity_direct.as_raw_fd())).build()).await == 0);
                let mut actual = vec![0;probe_len as usize];
                parity_file.as_file().read_exact_at(&mut actual,0)?;
                ensure!(actual == expected, "mixed parity backend mismatch");
                eprintln!("PASS: qid={qid} tag=0 len={probe_len} ordinary Read into arena oldD/oldP -> USER/USER/KFIXED XOR3 -> arena ordinary Write+flush, independent parity verified");

            }
            if op == libublk::sys::UBLK_IO_OP_READ || op == libublk::sys::UBLK_IO_OP_WRITE {
                let mut pos = 0;
                while pos < bytes {
                    let len = (bytes - pos).min(4096);
                    let sqe = if op == libublk::sys::UBLK_IO_OP_WRITE {
                        opcode::WriteFixed::new(types::Fixed(1), pos as usize as *const u8, len, 0)
                            .offset(off + pos as u64)
                            .build()
                    } else {
                        opcode::ReadFixed::new(types::Fixed(1), pos as usize as *mut u8, len, 0)
                            .offset(off + pos as u64)
                            .build()
                    };
                    let n = q1.ublk_submit_sqe(sqe).await;
                    if n != len as i32 {
                        result = if n < 0 { n } else { -libc::EIO };
                        break;
                    }
                    pos += len;
                }
            } else if op == libublk::sys::UBLK_IO_OP_FLUSH {
                result = q1
                    .ublk_submit_sqe(opcode::Fsync::new(types::Fixed(1)).build())
                    .await;
            } else {
                result = -libc::EOPNOTSUPP;
            }
            Ok(result)
            }.await;
            let result=match request_result {
                Ok(n)=>n,
                Err(e)=>{eprintln!("request failed after submitted CQEs drained: {e:#}");-libc::EIO},
            };
            NORMAL_IO_COUNT.fetch_add(1,std::sync::atomic::Ordering::Release);
            q1.submit_io_commit_cmd(0, desc.clone(), result).await?;
        }
        #[allow(unreachable_code)]
        Ok::<(), anyhow::Error>(())
    });
    smol::block_on(async {
        libublk::wait_and_handle_io_events(
            &q,
            Some(1),
            || {
                while exe.try_tick() {}
                if task.is_finished() && !wake_stop.replace(true) {
                    wake.signal();
                }
                if task.is_finished() && !pending.is_finished() && cancel.borrow().is_none() {
                    let future = q.ublk_submit_sqe(opcode::AsyncCancel::new(pending_token).build());
                    *cancel.borrow_mut() = Some(exe.spawn(async move { future.await }));
                }
                while exe.try_tick() {}
            },
            || {
                task.is_finished()
                    && wake_task.is_finished()
                    && pending.is_finished()
                    && cancel.borrow().as_ref().is_none_or(|t| t.is_finished())
            },
        )
        .await
    })?;
    smol::block_on(wake_task)?;
    let original = smol::block_on(pending);
    ensure!(
        original == -libc::ECANCELED,
        "pending original CQE {original}"
    );
    if let Some(cancel) = cancel.into_inner() {
        ensure!(smol::block_on(cancel) == 0, "cancel CQE must be consumed");
    }
    eprintln!("DRAINED qid={qid}: original timeout CQE -ECANCELED and cancel CQE 0 consumed before link/map teardown");
    if !clear.is_finished() {
        let iov = libc::iovec {
            iov_base: std::ptr::null_mut(),
            iov_len: 0,
        };
        libublk::with_task_io_ring(|r| unsafe {
            r.submitter().register_buffers_update(0, &[iov], None)
        })?;
    }
    if clear.is_finished() {
        smol::block_on(clear)?;
    } else {
        smol::block_on(clear.cancel());
    }
    match smol::block_on(task) {
        Err(e)
            if e.downcast_ref::<UblkError>()
                .is_some_and(|e| matches!(e, UblkError::QueueIsDown)) =>
        {
            Ok(())
        }
        r => r,
    }
}
fn main() -> Result<()> {
    let args = std::env::args().collect::<Vec<_>>();
    ensure!(
        args.len() == 3,
        "usage: raid5_kfixed_probe ID NEW_TEST_FILE"
    );
    let id = args[1].parse::<i32>()?;
    let f = OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(libc::O_DIRECT)
        .open(&args[2])?;
    ensure!(
        f.metadata()?.is_file(),
        "spike accepts regular disposable files only"
    );
    let lock_deadline = std::time::Instant::now() + std::time::Duration::from_secs(5);
    while unsafe { libc::flock(f.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        ensure!(
            std::time::Instant::now() < lock_deadline,
            "old backend file/ring ownership not released"
        );
        std::thread::sleep(std::time::Duration::from_millis(10));
    }
    let size = f.metadata()?.len();
    let recovering = std::env::var_os("RAID5_SPIKE_RECOVER").is_some();
    if recovering {
        let existing = UblkCtrl::new_simple(id)?;
        ensure!(
            existing.try_start_user_recover()? == 0,
            "takeover refused; old worker not fenced"
        );
    }
    let ctrl = UblkCtrlBuilder::default()
        .name("raid5-kfixed-spike")
        .id(id)
        .depth(1)
        .nr_queues(
            std::env::var("RAID5_SPIKE_QUEUES")
                .unwrap_or_else(|_| "1".into())
                .parse()?,
        )
        .io_buf_bytes(131072)
        .ctrl_flags((libublk::sys::UBLK_F_AUTO_BUF_REG | libublk::sys::UBLK_F_USER_RECOVERY) as u64)
        .dev_flags(if recovering {
            UblkFlags::UBLK_DEV_F_RECOVER_DEV
        } else {
            UblkFlags::UBLK_DEV_F_ADD_DEV
        })
        .build()?;
    let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    let sync = if std::env::var_os("RAID5_SPIKE_SYNC").is_some() {
        let stop = stop.clone();
        Some(std::thread::spawn(move || -> Result<()> {
            let mut rounds = 0;
            let mut overlaps = 0;
            while !stop.load(std::sync::atomic::Ordering::Acquire) {
                let (gate, stripes) = coordination();
                let admission =
                    smol::block_on(gate.acquire(rublk::raid5::flush_gate::Mode::Shared));
                let stripe = smol::block_on(stripes.acquire(0))
                    .map_err(|e| anyhow::anyhow!("failed sync stripe {}", e.0))?;
                if sync_probe::run(Some(&NORMAL_IO_COUNT))? {
                    overlaps += 1;
                }
                drop(stripe);
                drop(admission);
                rounds += 1;
            }
            eprintln!("SYNC_DRAINED rounds={rounds} overlapping_io_rounds={overlaps}");
            ensure!(
                rounds > 0 && overlaps > 0,
                "no observed sync/normal overlap"
            );
            Ok(())
        }))
    } else {
        None
    };
    ctrl.run_target(
        |dev| {
            dev.set_default_params(size);
            dev.tgt.fds[1] = f.as_raw_fd();
            dev.tgt.nr_fds = 2;
            Ok(())
        },
        |qid, dev| {
            if let Err(e) = queue(qid, dev) {
                eprintln!("queue failed: {e:#}");
                std::process::exit(1);
            }
        },
        |c: &UblkCtrl| {
            eprintln!("READY {}", c.dev_info().dev_id);
        },
    )?;
    stop.store(true, std::sync::atomic::Ordering::Release);
    if let Some(worker) = sync {
        worker.join().expect("sync probe worker panic")?;
    }
    Ok(())
}
