// SPDX-License-Identifier: GPL-2.0-or-later
//! Dynamic production runtime arena/tag isolation, real BPF CQEs, no target device.
use anyhow::{ensure, Result};
use io_uring::{bpf::BufferDescriptor, opcode, types};
use rublk::raid5::bpf_runtime::BpfRuntime;
use std::{mem::MaybeUninit, os::fd::AsRawFd};
fn submit(sqe: io_uring::squeue::Entry) -> Result<i32> {
    let executor = smol::LocalExecutor::new();
    let task = executor.spawn(async move {
        libublk::uring_async::ublk_submit_sqe_async(sqe, libublk::UblkUringData::Target as u64)
            .await
    });
    while !task.is_finished() {
        while executor.try_tick() {}
        if task.is_finished() {
            break;
        }
        libublk::with_task_io_ring_mut(|ring| -> Result<()> {
            ring.submit_and_wait(1)?;
            libublk::uring_async::ublk_reap_events_with_handler(ring, |c| {
                libublk::uring_async::ublk_wake_task(c.user_data(), c)
            })?;
            Ok(())
        })?;
    }
    Ok(smol::block_on(task)?)
}
fn main() -> Result<()> {
    libublk::io::ublk_init_bpf_task_ring(64, 128)?;
    let fd = libublk::with_task_io_ring(|r| r.as_raw_fd());
    for depth in [1, 8, 128, 256] {
        let mut storage = MaybeUninit::uninit();
        let runtime = BpfRuntime::load(&mut storage, fd, depth)?;
        let first = if depth > 1 {
            Some(runtime.tag(0)?)
        } else {
            None
        };
        if let Some(first) = &first {
            unsafe {
                std::ptr::write_bytes(first.address(first.slots().new_parity, 65536)?, 0xa5, 65536);
            }
        }
        let mut tag = runtime.tag(depth - 1)?;
        ensure!(
            runtime.tag(depth - 1).is_err(),
            "same tag concurrently leased"
        );
        ensure!(runtime.tag(depth).is_err(), "out of bounds tag accepted");
        let mut a = libublk::helpers::IoBuf::<u8>::new(65536);
        let mut b = libublk::helpers::IoBuf::<u8>::new(65536);
        // Source buffers initialized without any software parity computation.
        a.as_mut_slice().fill(0x35);
        b.as_mut_slice().fill(0x96);
        let source = [
            BufferDescriptor {
                len: 65536,
                addr: a.as_ptr() as u64,
                ..Default::default()
            },
            BufferDescriptor {
                len: 65536,
                addr: b.as_ptr() as u64,
                ..Default::default()
            },
        ];
        // SAFETY: tag, runtime and source allocations retained through exact CQE.
        let sqe = unsafe { tag.xor_sqe(&source, 65536)? };
        ensure!(submit(sqe)? == 65536, "short BPF CQE");
        let parity = tag.address(tag.slots().new_parity, 65536)?;
        ensure!(
            unsafe { std::slice::from_raw_parts(parity, 65536) }
                .iter()
                .all(|b| *b == 0xa3),
            "parity mismatch"
        );
        if let Some(first) = &first {
            ensure!(
                unsafe {
                    std::slice::from_raw_parts(
                        first.address(first.slots().new_parity, 65536)?,
                        65536,
                    )
                }
                .iter()
                .all(|b| *b == 0xa5),
                "cross tag arena corruption"
            );
        }
        let tmp = tempfile::NamedTempFile::new()?;
        tmp.as_file().set_len(65536)?;
        use std::os::unix::fs::OpenOptionsExt;
        let file = std::fs::OpenOptions::new()
            .read(true)
            .write(true)
            .custom_flags(libc::O_DIRECT)
            .open(tmp.path())?;
        ensure!(
            submit(opcode::Write::new(types::Fd(file.as_raw_fd()), parity, 65536).build())?
                == 65536
        );
        ensure!(submit(opcode::Fsync::new(types::Fd(file.as_raw_fd())).build())? == 0);
        ensure!(
            std::fs::read(tmp.path())? == vec![0xa3; 65536],
            "direct arena write mismatch"
        );
        println!("PASS depth={depth} arena_bytes={} map_id={} last_tag={} distinct live leases, exact BPF CQE and ordinary direct Write",runtime.bytes(),runtime.map_id()?,depth-1);
    }
    let mut storage = MaybeUninit::uninit();
    ensure!(
        BpfRuntime::load(&mut storage, fd, 342).is_err(),
        "arena cap exceeded"
    );
    Ok(())
}
