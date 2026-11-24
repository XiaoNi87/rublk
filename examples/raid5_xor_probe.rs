// SPDX-License-Identifier: GPL-2.0-or-later
//! USER XOR + arena/direct-I/O gate on the pinned kernel; no RAID5 device.
use anyhow::{ensure, Result};
use io_uring::{bpf::BufferDescriptor, opcode, types};
use libbpf_rs::{
    skel::{OpenSkel, SkelBuilder},
    AsRawLibbpf,
};
use std::{
    fs::OpenOptions,
    io::{Read, Seek, SeekFrom},
    mem::MaybeUninit,
    os::{fd::AsRawFd, unix::fs::OpenOptionsExt},
};
#[path = "../src/raid5/buffer.rs"]
mod buffer;
mod skeleton {
    include!(concat!(env!("OUT_DIR"), "/xor.skel.rs"));
}
#[repr(C)]
#[derive(Default)]
struct Descriptor {
    nr: u32,
    len: u32,
    output: u32,
    reserved: u32,
    source: [BufferDescriptor; 3],
}
struct Mapping {
    ptr: *mut u8,
}
// Borrowed mapping; libbpf owns munmap, after all CQEs drain.
fn submit(sqe: io_uring::squeue::Entry) -> Result<i32> {
    let ex = smol::LocalExecutor::new();
    let t = ex.spawn(async move {
        libublk::uring_async::ublk_submit_sqe_async(sqe, libublk::UblkUringData::Target as u64)
            .await
    });
    while !t.is_finished() {
        while ex.try_tick() {}
        if t.is_finished() {
            break;
        }
        libublk::with_task_io_ring_mut(|r| -> Result<()> {
            r.submit_and_wait(1)?;
            libublk::uring_async::ublk_reap_events_with_handler(r, |c| {
                libublk::uring_async::ublk_wake_task(c.user_data(), c)
            })?;
            Ok(())
        })?;
    }
    Ok(smol::block_on(t)?)
}
fn main() -> Result<()> {
    run(None).map(|_| ())
}
pub(crate) fn run(counter: Option<&std::sync::atomic::AtomicU64>) -> Result<bool> {
    let before = counter
        .map(|v| v.load(std::sync::atomic::Ordering::Acquire))
        .unwrap_or(0);
    let quiet = counter.is_some();
    macro_rules! report {($($args:tt)*)=>{if !quiet {println!($($args)*);}};}

    ensure!(
        unsafe { libc::sysconf(libc::_SC_PAGESIZE) } == 4096,
        "probe expects 4 KiB kernel pages"
    );
    libublk::io::ublk_init_bpf_task_ring(16, 32)?;
    let budget = buffer::BufferBudget::new(1, 1, 1, 1 << 20)?;
    ensure!(budget.normal_slots(0)?.new_parity + buffer::CHUNK == budget.normal_ring_bytes);
    report!(
        "scratch normal={} sync={} total={} descriptors={} kernel-request-bound={} sync-last={:?}",
        budget.normal_ring_bytes,
        budget.sync_ring_bytes,
        budget.arena_bytes,
        budget.descriptor_bytes,
        budget.kernel_request_bytes,
        budget.sync_slots(0)?
    );
    let fd = libublk::with_task_io_ring(|r| r.as_raw_fd());
    let mut object = MaybeUninit::uninit();
    let open = skeleton::XorSkelBuilder::default().open(&mut object)?;
    unsafe {
        (*open.struct_ops.xor_ops).ring_fd = fd;
    }
    let mut skel = open.load()?;
    let size = budget.normal_ring_bytes;
    // libbpf already owns and maps the arena during object load. Re-mapping
    // at another address is forbidden by the arena ABI. The pinned getter
    // returns its mmap base (arena_data_sz is zero without arena globals).
    let mut ignored_size = 0;
    let addr = unsafe {
        libbpf_rs::libbpf_sys::bpf_map__initial_value(
            skel.maps.arena.as_libbpf_object().as_ptr(),
            &mut ignored_size,
        )
    };
    ensure!(!addr.is_null(), "libbpf arena mapping absent");
    let arena = Mapping { ptr: addr.cast() };
    unsafe {
        std::ptr::write_bytes(arena.ptr, 0xcc, size);
    }
    skel.maps.bss_data.as_mut().unwrap().arena_base = arena.ptr;
    let link = skel.maps.xor_ops.attach_struct_ops()?;
    let mut bufs = (0..3)
        .map(|_| libublk::helpers::IoBuf::<u8>::new(69632))
        .collect::<Vec<_>>();
    for (s, b) in bufs.iter_mut().enumerate() {
        for (i, v) in b.as_mut_slice().iter_mut().enumerate() {
            *v = ((i * (13 + s * 2) + s * 17) ^ ((i >> 8) * (29 + s * 4)) ^ ((i >> 12) * 71)) as u8;
        }
    }
    let temp = tempfile::NamedTempFile::new()?;
    temp.as_file().set_len(65536)?;
    let backend = OpenOptions::new()
        .read(true)
        .write(true)
        .custom_flags(libc::O_DIRECT)
        .open(temp.path())?;
    for nr in [2, 3] {
        for len in [512, 4096, 65536] {
            for offsets in [[0, 0, 0], [512, 1024, 3584]] {
                let mut d = Descriptor {
                    nr,
                    len,
                    output: 65536,
                    ..Default::default()
                };
                let mut expected = vec![0; len as usize];
                for s in 0..nr as usize {
                    let offset = offsets[s];
                    d.source[s] = BufferDescriptor {
                        len,
                        addr: bufs[s].as_ptr() as u64 + offset as u64,
                        ..Default::default()
                    };
                    for (i, v) in expected.iter_mut().enumerate() {
                        *v ^= bufs[s].as_slice()[offset + i];
                    }
                }
                unsafe {
                    std::ptr::write_bytes(arena.ptr, 0xcc, size);
                }
                let cqe = submit(opcode::Bpf::new((&d as *const Descriptor).cast(), 1).build())?;
                ensure!(
                    cqe == len as i32,
                    "XOR nr={nr} len={len} offsets={offsets:?}: CQE {cqe}"
                );
                let got = unsafe {
                    std::slice::from_raw_parts(arena.ptr.add(d.output as usize), len as usize)
                };
                ensure!(got == expected, "XOR bytes mismatch");
                let all = unsafe { std::slice::from_raw_parts(arena.ptr, size) };
                ensure!(
                    all[..65536].iter().all(|b| *b == 0xcc)
                        && all[65536 + len as usize..].iter().all(|b| *b == 0xcc),
                    "arena guard changed"
                );
                let n = submit(
                    opcode::Write::new(types::Fd(backend.as_raw_fd()), got.as_ptr(), len)
                        .offset(0)
                        .build(),
                )?;
                ensure!(n == len as i32, "arena ordinary O_DIRECT Write CQE {n}");
                ensure!(submit(opcode::Fsync::new(types::Fd(backend.as_raw_fd())).build())? == 0);
                let mut read = temp.reopen()?;
                read.seek(SeekFrom::Start(0))?;
                let mut actual = vec![0; len as usize];
                read.read_exact(&mut actual)?;
                ensure!(actual == expected, "arena backend readback mismatch");
                report!("PASS: USER XOR{nr} len={len} offsets={offsets:?}, exact CQE/guards, arena ordinary O_DIRECT Write+flush+independent readback");
            }
        }
    }
    let valid = BufferDescriptor {
        len: 4096,
        addr: bufs[0].as_ptr() as u64,
        ..Default::default()
    };
    for case in 0..6 {
        let mut d = Descriptor {
            nr: 2,
            len: 4096,
            output: 0,
            source: [valid; 3],
            ..Default::default()
        };
        match case {
            0 => d.nr = 4,
            1 => d.len = 511,
            2 => d.output = u32::MAX,
            3 => d.source[0].addr = 0,
            4 => d.source[0].kind = 2,
            _ => d.source[0].len = 512,
        }
        let cqe = submit(opcode::Bpf::new((&d as *const Descriptor).cast(), 1).build())?;
        ensure!(cqe < 0, "invalid XOR descriptor case={case} returned {cqe}");
        report!("PASS: invalid XOR case={case} rejected CQE={cqe}; no backend submission");
    }
    // Fault after a valid first page: partial arena changes must never be
    // treated as a complete parity result or submitted to a backend.
    let fault = unsafe {
        libc::mmap(
            std::ptr::null_mut(),
            8192,
            libc::PROT_READ | libc::PROT_WRITE,
            libc::MAP_PRIVATE | libc::MAP_ANONYMOUS,
            -1,
            0,
        )
    };
    ensure!(fault != libc::MAP_FAILED);
    unsafe {
        std::ptr::write_bytes(fault, 0x55, 8192);
    }
    ensure!(
        unsafe { libc::mprotect((fault as *mut u8).add(4096).cast(), 4096, libc::PROT_NONE) } == 0
    );
    let mut d = Descriptor {
        nr: 2,
        len: 8192,
        output: 0,
        ..Default::default()
    };
    d.source[0] = BufferDescriptor {
        len: 8192,
        addr: fault as u64,
        ..Default::default()
    };
    d.source[1] = BufferDescriptor {
        len: 8192,
        addr: bufs[1].as_ptr() as u64,
        ..Default::default()
    };
    let n = submit(opcode::Bpf::new((&d as *const Descriptor).cast(), 1).build())?;
    ensure!(n < 0, "inaccessible second page accepted CQE={n}");
    ensure!(unsafe { libc::munmap(fault, 8192) } == 0);
    report!("PASS: USER extraction fault after first page returns {n}, no backend submission, allocation retained through CQE");
    // A valid file-backed descriptor with no user PTE must be faulted in by
    // the sleepable issue callback. Never dereference it before submission.
    let cold = tempfile::NamedTempFile::new()?;
    cold.as_file().set_len(4096)?;
    let d = Descriptor {
        nr: 2,
        len: 4096,
        output: 0,
        source: [BufferDescriptor {
            len: 4096,
            addr: bufs[0].as_ptr() as u64,
            ..Default::default()
        }; 3],
        ..Default::default()
    };
    let raw = unsafe {
        std::slice::from_raw_parts(
            (&d as *const Descriptor).cast::<u8>(),
            std::mem::size_of::<Descriptor>(),
        )
    };
    std::os::unix::fs::FileExt::write_all_at(cold.as_file(), raw, 0)?;
    let ptr = unsafe {
        libc::mmap(
            std::ptr::null_mut(),
            4096,
            libc::PROT_READ,
            libc::MAP_PRIVATE,
            cold.as_file().as_raw_fd(),
            0,
        )
    };
    ensure!(ptr != libc::MAP_FAILED);
    let pagemap = std::fs::File::open("/proc/self/pagemap")?;
    let mut entry = [0u8; 8];
    std::os::unix::fs::FileExt::read_exact_at(&pagemap, &mut entry, ((ptr as u64) / 4096) * 8)?;
    ensure!(
        u64::from_le_bytes(entry) >> 63 == 0,
        "cold descriptor already has a present PTE"
    );
    let n = submit(opcode::Bpf::new(ptr.cast(), 1).build())?;
    ensure!(unsafe { libc::munmap(ptr, 4096) } == 0);
    if std::env::var_os("RAID5_EXPECT_COLD_DESCRIPTOR_FAULT").is_some() {
        ensure!(
            n == -libc::EFAULT,
            "negative control: cold descriptor CQE={n}"
        );
        report!("PASS: old nofault helper rejects valid absent-PTE descriptor with EFAULT (negative control)");
    } else {
        ensure!(n == 4096, "valid absent-PTE descriptor CQE={n}");
        ensure!(
            unsafe { std::slice::from_raw_parts(arena.ptr, 4096) }
                .iter()
                .all(|v| *v == 0),
            "cold descriptor XOR mismatch"
        );
        report!(
            "PASS: valid absent-PTE descriptor faulted in, exact CQE and independent XOR bytes"
        );
    }
    for ptr in [std::ptr::null(), 1usize as *const u8] {
        let n = submit(opcode::Bpf::new(ptr, 1).build())?;
        ensure!(n == -libc::EFAULT, "invalid descriptor CQE={n}");
        report!("PASS: inaccessible descriptor rejected CQE={n}");
    }
    drop(link);
    drop(arena);
    drop(skel);
    let after = counter
        .map(|v| v.load(std::sync::atomic::Ordering::Acquire))
        .unwrap_or(0);
    Ok(after > before)
}
