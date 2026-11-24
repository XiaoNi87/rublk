// SPDX-License-Identifier: GPL-2.0-or-later
//! Run as root on the pinned development kernel. No devices are created.
use anyhow::{ensure, Result};
use libbpf_rs::{
    skel::{OpenSkel, SkelBuilder},
    MapCore, MapFlags,
};
use libublk::{with_task_io_ring, with_task_io_ring_mut, UblkUringData};
use std::{mem::MaybeUninit, os::fd::AsRawFd};
mod skeleton {
    include!(concat!(env!("OUT_DIR"), "/probe.skel.rs"));
}
fn main() -> Result<()> {
    std::thread::spawn(|| -> Result<()> {
        libublk::ublk_init_task_ring(|cell| {
            let ring = io_uring::IoUring::new(8).map_err(libublk::UblkError::IOError)?;
            cell.set(std::cell::RefCell::new(ring))
                .map_err(|_| libublk::UblkError::InvalidVal)
        })?;
        ensure!(
            libublk::io::ublk_init_bpf_task_ring(16, 32).is_err(),
            "existing default ring accepted"
        );
        libublk::with_task_io_ring_mut(|r| -> Result<()> {
            let e = io_uring::opcode::Bpf::new(std::ptr::null(), 1)
                .build()
                .user_data(7);
            // SAFETY: scalar probe has no referenced user buffers.
            unsafe {
                r.submission().push(&e)?;
            }
            r.submit_and_wait(1)?;
            let c = r.completion().next().expect("unsupported BPF CQE");
            ensure!(
                c.result() == -libc::EOPNOTSUPP,
                "default ring BPF result {}",
                c.result()
            );
            Ok(())
        })?;
        Ok(())
    })
    .join()
    .expect("default-ring check thread")?;

    libublk::io::ublk_init_bpf_task_ring(16, 32)?;
    libublk::io::ublk_init_bpf_task_ring(16, 32)?;
    ensure!(libublk::io::ublk_init_bpf_task_ring(32, 64).is_err());
    let fd = with_task_io_ring(|r| r.as_raw_fd());
    let mut object = MaybeUninit::uninit();
    let open = skeleton::ProbeSkelBuilder::default().open(&mut object)?;
    // SAFETY: pointer refers to the skeleton-owned initial struct_ops value,
    // mutable before load; the ring remains alive for the entire attachment.
    unsafe {
        (*open.struct_ops.probe_ops).ring_fd = fd;
    }
    let mut loaded = open.load()?;
    let link = loaded.maps.probe_ops.attach_struct_ops()?;
    ensure!(
        loaded.maps.probe_ops.attach_struct_ops().is_err(),
        "duplicate attachment accepted"
    );
    let exe = smol::LocalExecutor::new();
    let task = exe.spawn(async {
        use libublk::uring_async::ublk_submit_sqe_async;
        let ok = ublk_submit_sqe_async(
            io_uring::opcode::Bpf::new(std::ptr::null(), 1).build(),
            UblkUringData::Target as u64,
        )
        .await?;
        ensure!(ok == 64, "unexpected BPF CQE {ok}");
        let bad = ublk_submit_sqe_async(
            io_uring::opcode::Bpf::new(std::ptr::null(), 0).build(),
            UblkUringData::Target as u64,
        )
        .await?;
        ensure!(
            bad == -libc::EINVAL,
            "unexpected invalid descriptor CQE {bad}"
        );
        Ok::<_, anyhow::Error>(())
    });
    while !task.is_finished() {
        while exe.try_tick() {}
        if task.is_finished() {
            break;
        }
        with_task_io_ring_mut(|ring| -> Result<()> {
            ring.submit_and_wait(1)?;
            libublk::uring_async::ublk_reap_events_with_handler(ring, |cqe| {
                libublk::uring_async::ublk_wake_task(cqe.user_data(), cqe);
            })?;
            Ok(())
        })?;
    }
    smol::block_on(task)?;
    let output = loaded
        .maps
        .output
        .lookup(&0u32.to_ne_bytes(), MapFlags::ANY)?
        .expect("output map");
    ensure!(
        output == (0..64u8).map(|v| v ^ 0xa5).collect::<Vec<_>>(),
        "BPF output bytes mismatch"
    );
    drop(link);
    drop(loaded);
    let mut failed_object = MaybeUninit::uninit();
    let bad = skeleton::ProbeSkelBuilder::default().open(&mut failed_object)?;
    // SAFETY: same generated pre-load storage; deliberately invalid fd.
    unsafe {
        (*bad.struct_ops.probe_ops).ring_fd = -1;
    }
    match bad.load() {
        Err(_) => (),
        Ok(mut loaded) => ensure!(
            loaded.maps.probe_ops.attach_struct_ops().is_err(),
            "invalid ring fd attached"
        ),
    }

    println!("PASS: libublk BPF ring, Rust skeleton struct_ops attach, exact CQE/output 64 bytes, prep failure -EINVAL, incompatible init rejected");
    Ok(())
}
