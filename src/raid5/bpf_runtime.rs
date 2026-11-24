// SPDX-License-Identifier: GPL-2.0-or-later
//! One owning thread/ring/object/arena. No software parity fallback.
use super::buffer::{BufferBudget, Slots, CHUNK};
use anyhow::{ensure, Context, Result};
use io_uring::{bpf::BufferDescriptor, opcode, squeue};
use libbpf_rs::{
    skel::{OpenSkel, SkelBuilder},
    AsRawLibbpf, MapCore,
};
use std::{
    cell::{RefCell, RefMut},
    marker::PhantomData,
    mem::MaybeUninit,
    rc::Rc,
};
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
const _: () = assert!(std::mem::size_of::<Descriptor>() == 64);
pub struct BpfRuntime<'obj> {
    // Field order: detach first, then close maps/unmap arena. Owner drains first.
    _link: libbpf_rs::Link,
    skel: skeleton::XorSkel<'obj>,
    arena: usize,
    bytes: usize,
    descriptors: Box<[RefCell<Descriptor>]>,
    budget: BufferBudget,
    _queue_local: PhantomData<Rc<()>>,
}
impl<'obj> BpfRuntime<'obj> {
    pub fn load(
        storage: &'obj mut MaybeUninit<libbpf_rs::OpenObject>,
        ring_fd: i32,
        depth: usize,
    ) -> Result<Self> {
        ensure!(
            unsafe { libc::sysconf(libc::_SC_PAGESIZE) } == 4096,
            "BPF arena requires validated 4 KiB kernel page size"
        );
        let budget = BufferBudget::new(1, depth, 1, usize::MAX)?;
        let bytes = budget.normal_ring_bytes;
        let mut open = skeleton::XorSkelBuilder::default().open(storage)?;
        open.maps
            .arena
            .set_max_entries((bytes / 4096).try_into()?)?;
        open.maps
            .rodata_data
            .as_mut()
            .context("BPF rodata missing")?
            .arena_bytes = bytes.try_into()?;
        // SAFETY: generated pre-load struct_ops storage; sole owner on this thread.
        unsafe {
            (*open.struct_ops.xor_ops).ring_fd = ring_fd;
        }
        let mut skel = open.load()?;
        let mut ignored = 0;
        // libbpf owns this mapping. No second mmap, and no caller munmap.
        let arena = unsafe {
            libbpf_rs::libbpf_sys::bpf_map__initial_value(
                skel.maps.arena.as_libbpf_object().as_ptr(),
                &mut ignored,
            )
        } as usize;
        ensure!(
            arena != 0 && arena % 4096 == 0,
            "arena mapping missing or unaligned"
        );
        // Arena faults may fail opportunistic kernel allocations even when
        // ordinary free memory remains. Fault pages through a syscall first:
        // MADV_POPULATE_WRITE converts VM_FAULT_SIGSEGV into an errno instead
        // of terminating the process during the first userspace memset.
        // Do not attach the program or expose any buffers on partial failure.
        // A failed population may leave only a prefix resident. Retrying
        // population of this same private mapping is safe before attachment;
        // it neither resubmits a transaction nor publishes partial buffers.
        // Opportunistic allocation is not guaranteed to recover: bound both
        // attempts and delay, and propagate persistent failures to rollback.
        for attempt in 0..8 {
            let populated = unsafe {
                libc::madvise(arena as *mut libc::c_void, bytes, libc::MADV_POPULATE_WRITE)
            };
            if populated == 0 {
                break;
            }
            let error = std::io::Error::last_os_error();
            if attempt == 7
                || !matches!(
                    error.raw_os_error(),
                    Some(libc::EFAULT | libc::ENOMEM | libc::EINTR)
                )
            {
                return Err(error).context("populate BPF arena");
            }
            super::log(format_args!(
                "RAID5 arena population attempt {} failed: {}; retrying before attachment",
                attempt + 1,
                error
            ));
            std::thread::sleep(std::time::Duration::from_millis(10));
        }
        unsafe {
            std::ptr::write_bytes(arena as *mut u8, 0, bytes);
        }
        skel.maps
            .bss_data
            .as_mut()
            .context("BPF bss missing")?
            .arena_base = arena as *mut u8;
        let link = skel.maps.xor_ops.attach_struct_ops()?;
        Ok(Self {
            _link: link,
            skel,
            arena,
            bytes,
            descriptors: (0..depth)
                .map(|_| RefCell::new(Descriptor::default()))
                .collect(),
            budget,
            _queue_local: PhantomData,
        })
    }
    pub fn map_id(&self) -> Result<u32> {
        Ok(self.skel.maps.arena.info()?.info.id)
    }
    pub fn bytes(&self) -> usize {
        self.bytes
    }
    pub fn tag(&self, index: usize) -> Result<Tag<'_, 'obj>> {
        let descriptor = self
            .descriptors
            .get(index)
            .context("tag outside arena")?
            .try_borrow_mut()
            .context("tag transaction still owns scratch/descriptor")?;
        Ok(Tag {
            runtime: self,
            descriptor,
            slots: self.budget.normal_slots(index)?,
        })
    }
}
/// Keep this lease in the independently owned transaction until ALL original
/// backend/BPF/cancel CQEs drain. It excludes same-tag descriptor/arena reuse.
pub struct Tag<'a, 'obj> {
    runtime: &'a BpfRuntime<'obj>,
    descriptor: RefMut<'a, Descriptor>,
    slots: Slots,
}
impl Tag<'_, '_> {
    pub fn slots(&self) -> Slots {
        self.slots
    }
    /// Address only; no Rust reference aliases kernel/BPF writes.
    pub fn address(&self, offset: usize, len: usize) -> Result<*mut u8> {
        let start = self.slots.old_data;
        let end = self.slots.new_parity + CHUNK;
        ensure!(
            offset >= start && offset.checked_add(len).is_some_and(|n| n <= end),
            "buffer outside tag allocation"
        );
        Ok((self.runtime.arena + offset) as *mut u8)
    }
    /// # Safety
    /// Retain this lease, runtime and every source allocation until the original
    /// CQE is consumed. Do not overwrite this descriptor while its SQE is pending.
    /// Each source must remain accessible (KFIXED index belongs to this ring).
    pub unsafe fn xor_sqe(
        &mut self,
        sources: &[BufferDescriptor],
        len: u32,
    ) -> Result<squeue::Entry> {
        ensure!(
            (2..=3).contains(&sources.len()),
            "XOR requires two or three inputs"
        );
        ensure!(
            len > 0 && len as usize <= CHUNK && len % 512 == 0,
            "invalid XOR length"
        );
        for src in sources {
            ensure!(
                (src.kind == io_uring::bpf::USER || src.kind == io_uring::bpf::KFIXED)
                    && src.len == len
                    && src.reserved == 0
                    && src.addr % 512 == 0,
                "invalid XOR source"
            );
        }
        let mut d = Descriptor {
            nr: sources.len() as u32,
            len,
            output: self.slots.new_parity.try_into()?,
            ..Default::default()
        };
        d.source[..sources.len()].copy_from_slice(sources);
        *self.descriptor = d;
        Ok(opcode::Bpf::new((&*self.descriptor as *const Descriptor).cast(), 1).build())
    }
}
