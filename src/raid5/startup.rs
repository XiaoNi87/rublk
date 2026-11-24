// SPDX-License-Identifier: GPL-2.0-or-later
//! Bounded-memory read-only adoption check. Parity is computed only by BPF.
use super::{backend::Backends, bpf_runtime::BpfRuntime, geometry::CHUNK};
use anyhow::{ensure, Result};
use io_uring::{bpf::BufferDescriptor, opcode, types};
use std::{mem::MaybeUninit, os::fd::AsRawFd};
/// Startup-only synchronous driver of the libublk asynchronous CQE machinery.
/// Every returning path has consumed the original CQE (or submitted nothing).
pub fn submit(sqe: io_uring::squeue::Entry) -> Result<i32> {
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
        libublk::with_task_io_ring_mut(|ring| {
            loop {
                match ring.submit_and_wait(1) {
                    Ok(_) => break,
                    Err(e) if e.raw_os_error() == Some(libc::EINTR) => continue,
                    Err(e) => {
                        // Cannot return and free a submitted operation's descriptor/buffer.
                        super::log(format_args!(
                            "fatal: startup ring wait failed before original CQE drain: {e}"
                        ));
                        std::process::abort();
                    }
                }
            }
            if let Err(e) = libublk::uring_async::ublk_reap_events_with_handler(ring, |c| {
                libublk::uring_async::ublk_wake_task(c.user_data(), c)
            }) {
                super::log(format_args!("fatal: startup CQE routing failed: {e}"));
                std::process::abort();
            }
        });
    }
    Ok(smol::block_on(task)?)
}
pub fn verify_adoption(backends: &Backends) -> Result<()> {
    libublk::io::ublk_init_bpf_task_ring(64, 128)?;
    let fd = libublk::with_task_io_ring(|r| r.as_raw_fd());
    let mut storage = MaybeUninit::uninit();
    let runtime = BpfRuntime::load(&mut storage, fd, 1)?;
    let mut tag = runtime.tag(0)?;
    let slots = tag.slots();
    let files = backends.fds();
    for stripe in 0..backends.geometry.stripes() {
        let segments = backends.geometry.split(stripe * 2 * CHUNK, 2 * CHUNK)?;
        let offset = segments[0].backend_offset;
        let d0 = tag.address(slots.old_data, CHUNK as usize)?;
        let d1 = tag.address(slots.old_parity, CHUNK as usize)?;
        ensure!(
            submit(
                opcode::Read::new(
                    types::Fd(files[segments[0].data_disk as usize]),
                    d0,
                    CHUNK as u32
                )
                .offset(offset)
                .build()
            )? == CHUNK as i32,
            "short/error adoption data0 read at stripe {stripe}"
        );
        ensure!(
            submit(
                opcode::Read::new(
                    types::Fd(files[segments[1].data_disk as usize]),
                    d1,
                    CHUNK as u32
                )
                .offset(offset)
                .build()
            )? == CHUNK as i32,
            "short/error adoption data1 read at stripe {stripe}"
        );
        let sources = [
            BufferDescriptor {
                len: CHUNK as u32,
                addr: d0 as u64,
                ..Default::default()
            },
            BufferDescriptor {
                len: CHUNK as u32,
                addr: d1 as u64,
                ..Default::default()
            },
        ];
        // SAFETY: tag/runtime/input arena allocations retained, descriptor unchanged
        // until submit consumes the original BPF CQE; no concurrent owner exists.
        let sqe = unsafe { tag.xor_sqe(&sources, CHUNK as u32)? };
        ensure!(
            submit(sqe)? == CHUNK as i32,
            "short/error adoption XOR at stripe {stripe}"
        );
        // Inputs have been consumed; reuse old-data slot for stored parity.
        ensure!(
            submit(
                opcode::Read::new(
                    types::Fd(files[segments[0].parity_disk as usize]),
                    d0,
                    CHUNK as u32
                )
                .offset(offset)
                .build()
            )? == CHUNK as i32,
            "short/error adoption parity read at stripe {stripe}"
        );
        let computed = tag.address(slots.new_parity, CHUNK as usize)?;
        let same = unsafe {
            std::slice::from_raw_parts(d0, CHUNK as usize)
                == std::slice::from_raw_parts(computed, CHUNK as usize)
        };
        ensure!(
            same,
            "initial parity inconsistent at stripe {stripe}; no member has been modified"
        );
    }
    Ok(())
}
