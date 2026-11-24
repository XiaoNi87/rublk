// SPDX-License-Identifier: GPL-2.0-or-later
//! Parity transactions shared by normal queues and the independent sync ring.
//! The queue registry owns these futures until every original CQE is consumed.
use super::{
    bpf_runtime::{BpfRuntime, Tag},
    flush_gate::{FlushGate, Mode},
    geometry::{Geometry, Segment, CHUNK},
    queue_wake::QueueWake,
    recovery::Frontier,
    stripe::{StripePermit, StripeWorkTable},
    transaction::Cancellation,
};
use anyhow::{ensure, Result};
use io_uring::{bpf::BufferDescriptor, opcode, squeue, types};
use std::sync::{
    atomic::{AtomicBool, AtomicU64, Ordering},
    Mutex,
};
pub struct Shared {
    pub sync_control: super::sync_control::SyncControl,
    pub geometry: Geometry,
    pub gate: FlushGate,
    pub stripes: StripeWorkTable,
    pub frontier: Mutex<Frontier>,
    pub stop: AtomicBool,
    pub error: AtomicBool,
    pub failed_transactions: AtomicU64,
    pub sync_started_ns: AtomicU64,
    pub sync_scan_completed_ns: AtomicU64,
    pub sync_finalization_started_ns: AtomicU64,
    pub sync_finalized: AtomicBool,
    pub sync_completed_ns: AtomicU64,
    wakes: Mutex<Vec<super::queue_wake::WeakQueueWake>>,
}
impl Shared {
    pub fn new(
        geometry: Geometry,
        shards: usize,
        window: usize,
        clean_baseline: bool,
    ) -> Result<Self> {
        ensure!(shards.is_power_of_two(), "invalid shard count");
        let frontier = if clean_baseline {
            Frontier::clean_baseline(geometry.stripes(), window as u64)?
        } else {
            Frontier::new(geometry.stripes(), window as u64)?
        };
        Ok(Self {
            sync_control: Default::default(),
            geometry,
            gate: FlushGate::default(),
            stripes: StripeWorkTable::new(shards),
            frontier: Mutex::new(frontier),
            stop: AtomicBool::new(false),
            error: AtomicBool::new(false),
            failed_transactions: AtomicU64::new(0),
            sync_started_ns: AtomicU64::new(0),
            sync_scan_completed_ns: AtomicU64::new(0),
            sync_finalization_started_ns: AtomicU64::new(0),
            sync_finalized: AtomicBool::new(clean_baseline),
            sync_completed_ns: AtomicU64::new(0),
            wakes: Mutex::new(Vec::new()),
        })
    }
    pub fn start_sync_clock(&self) {
        if !self.sync_done() {
            let _ = self.sync_started_ns.compare_exchange(
                0,
                now_ns(),
                Ordering::AcqRel,
                Ordering::Acquire,
            );
        }
    }
    fn complete_stripe(&self, stripe: u64) -> Result<()> {
        let done = {
            let mut f = self.frontier.lock().unwrap();
            f.complete(stripe)?;
            f.done()
        };
        if done && self.sync_started_ns.load(Ordering::Acquire) != 0 {
            let _ = self.sync_scan_completed_ns.compare_exchange(
                0,
                now_ns(),
                Ordering::AcqRel,
                Ordering::Acquire,
            );
        }
        Ok(())
    }
    pub fn register_wake(&self, wake: &QueueWake) {
        self.wakes.lock().unwrap().push(wake.downgrade());
    }
    /// Real CQE errors remain errors even when shutdown/cancellation raced them.
    /// A stop flag does not prove that a returned error was cancellation.
    pub fn transaction_error(&self, error: &anyhow::Error) -> bool {
        if error.is::<super::transaction::BeforeIoCancelled>() {
            return false;
        }
        self.error.store(true, Ordering::Release);
        self.request_stop();
        true
    }
    pub fn request_stop(&self) {
        self.stop.store(true, Ordering::Release);
        let wakes: Vec<_> = self
            .wakes
            .lock()
            .unwrap()
            .iter()
            .filter_map(|w| w.upgrade())
            .collect();
        for wake in wakes {
            wake.signal();
        }
    }
    pub fn first_unsynced(&self) -> u64 {
        self.frontier.lock().unwrap().first_unsynced()
    }
    pub fn scan_done(&self) -> bool {
        self.frontier.lock().unwrap().done()
    }
    pub fn sync_done(&self) -> bool {
        self.sync_finalized.load(Ordering::Acquire)
    }
    fn failed(&self, permit: &StripePermit) {
        permit.mark_failed();
        self.error.store(true, Ordering::Release);
        self.failed_transactions.fetch_add(1, Ordering::Relaxed);
    }
}
async fn io(sqe: squeue::Entry) -> Result<i32> {
    let timer = super::metrics::Timer::start();
    let result =
        libublk::uring_async::ublk_submit_sqe_async(sqe, libublk::UblkUringData::Target as u64)
            .await;
    timer.record(super::metrics::Kind::Cqe);
    Ok(result?)
}
async fn exact(sqe: squeue::Entry, expected: u32, phase: &str) -> Result<()> {
    let timer = super::metrics::Timer::start();
    let n = io(sqe).await?;
    if phase.contains("BPF") {
        timer.record(super::metrics::Kind::BpfCqe);
    }
    ensure!(
        n == expected as i32,
        "{phase}: CQE {n}, expected {expected}"
    );
    Ok(())
}
async fn write(
    shared: &Shared,
    permit: &StripePermit,
    sqe: squeue::Entry,
    bytes: u32,
    phase: &str,
) -> Result<()> {
    let result = exact(sqe, bytes, phase).await;
    if result.is_err() {
        shared.failed(permit);
    }
    result
}
/// Caller already owns shared flush admission and the matching stripe permit.
/// Complete a full parity repair without releasing either between its phases.
async fn repair(
    shared: &Shared,
    permit: &StripePermit,
    tag: &mut Tag<'_, '_>,
    files: [u32; 3],
) -> Result<()> {
    let stripe = permit.stripe();
    let parts = shared.geometry.split(stripe * 2 * CHUNK, 2 * CHUNK)?;
    let offset = parts[0].backend_offset;
    let slots = tag.slots();
    let a = tag.address(slots.old_data, CHUNK as usize)?;
    let b = tag.address(slots.old_parity, CHUNK as usize)?;
    exact(
        opcode::Read::new(
            types::Fixed(files[parts[0].data_disk as usize]),
            a,
            CHUNK as u32,
        )
        .offset(offset)
        .build(),
        CHUNK as u32,
        "sync data0 read",
    )
    .await?;
    exact(
        opcode::Read::new(
            types::Fixed(files[parts[1].data_disk as usize]),
            b,
            CHUNK as u32,
        )
        .offset(offset)
        .build(),
        CHUNK as u32,
        "sync data1 read",
    )
    .await?;
    let sources = [
        BufferDescriptor {
            len: CHUNK as u32,
            addr: a as u64,
            ..Default::default()
        },
        BufferDescriptor {
            len: CHUNK as u32,
            addr: b as u64,
            ..Default::default()
        },
    ];
    // SAFETY: owned transaction retains tag/runtime; all original CQEs awaited.
    let sqe = unsafe { tag.xor_sqe(&sources, CHUNK as u32)? };
    exact(sqe, CHUNK as u32, "sync BPF XOR").await?;
    let parity = tag.address(slots.new_parity, CHUNK as usize)?;
    write(
        shared,
        permit,
        opcode::Write::new(
            types::Fixed(files[parts[0].parity_disk as usize]),
            parity,
            CHUNK as u32,
        )
        .offset(offset)
        .build(),
        CHUNK as u32,
        "sync parity write",
    )
    .await?;
    shared.complete_stripe(stripe)?;
    Ok(())
}
fn kfixed(index: u16, offset: u64, len: u32) -> BufferDescriptor {
    BufferDescriptor {
        kind: io_uring::bpf::KFIXED,
        buffer_index: index,
        len,
        addr: offset,
        ..Default::default()
    }
}
async fn full_stripe(
    shared: &Shared,
    permit: &StripePermit,
    tag: &mut Tag<'_, '_>,
    index: u16,
    parts: &[Segment],
    files: [u32; 3],
) -> Result<()> {
    let sources = [
        kfixed(index, parts[0].request_offset, CHUNK as u32),
        kfixed(index, parts[1].request_offset, CHUNK as u32),
    ];
    let sqe = unsafe { tag.xor_sqe(&sources, CHUNK as u32)? };
    exact(sqe, CHUNK as u32, "full-stripe BPF XOR").await?;
    for p in parts {
        write(
            shared,
            permit,
            opcode::WriteFixed::new(
                types::Fixed(files[p.data_disk as usize]),
                p.request_offset as usize as *const u8,
                p.len as u32,
                index,
            )
            .offset(p.backend_offset)
            .build(),
            p.len as u32,
            "full-stripe data write",
        )
        .await?;
    }
    let parity = tag.address(tag.slots().new_parity, CHUNK as usize)?;
    write(
        shared,
        permit,
        opcode::Write::new(
            types::Fixed(files[parts[0].parity_disk as usize]),
            parity,
            CHUNK as u32,
        )
        .offset(parts[0].backend_offset)
        .build(),
        CHUNK as u32,
        "full-stripe parity write",
    )
    .await?;
    shared.complete_stripe(permit.stripe())?;
    Ok(())
}
async fn rmw(
    shared: &Shared,
    permit: &StripePermit,
    tag: &mut Tag<'_, '_>,
    index: u16,
    p: &Segment,
    files: [u32; 3],
) -> Result<()> {
    let len = p.len as u32;
    let slots = tag.slots();
    let a = tag.address(slots.old_data, len as usize)?;
    let b = tag.address(slots.old_parity, len as usize)?;
    exact(
        opcode::Read::new(types::Fixed(files[p.data_disk as usize]), a, len)
            .offset(p.backend_offset)
            .build(),
        len,
        "RMW old data read",
    )
    .await?;
    exact(
        opcode::Read::new(types::Fixed(files[p.parity_disk as usize]), b, len)
            .offset(p.backend_offset)
            .build(),
        len,
        "RMW old parity read",
    )
    .await?;
    let sources = [
        BufferDescriptor {
            len,
            addr: a as u64,
            ..Default::default()
        },
        BufferDescriptor {
            len,
            addr: b as u64,
            ..Default::default()
        },
        kfixed(index, p.request_offset, len),
    ];
    let sqe = unsafe { tag.xor_sqe(&sources, len)? };
    exact(sqe, len, "RMW BPF XOR").await?;
    write(
        shared,
        permit,
        opcode::WriteFixed::new(
            types::Fixed(files[p.data_disk as usize]),
            p.request_offset as usize as *const u8,
            len,
            index,
        )
        .offset(p.backend_offset)
        .build(),
        len,
        "RMW data write",
    )
    .await?;
    // Once a mutation is submitted, cancellation cannot interrupt parity completion.
    let parity = tag.address(slots.new_parity, len as usize)?;
    write(
        shared,
        permit,
        opcode::Write::new(types::Fixed(files[p.parity_disk as usize]), parity, len)
            .offset(p.backend_offset)
            .build(),
        len,
        "RMW parity write",
    )
    .await?;
    Ok(())
}
/// # Safety
/// Must be held by the independent transaction registry; NEVER abort/drop this
/// future with a submitted CQE pending. Retain the KFIXED request until result.
pub async unsafe fn request(
    shared: &Shared,
    runtime: &BpfRuntime<'_>,
    wake: &QueueWake,
    cancel: Cancellation,
    index: u16,
    iod: libublk::sys::ublksrv_io_desc,
    files: [u32; 3],
    max_bytes: u32,
) -> Result<i32> {
    let op = iod.op_flags & 255;
    // FUA is not advertised. Also reject fallback-needed, integrity and foreign
    // shared-memory address descriptors from the pinned UAPI (bits 17/18/19).
    if iod.op_flags & (libublk::sys::UBLK_IO_F_FUA | (7 << 17)) != 0 {
        return Ok(-libc::EOPNOTSUPP);
    }
    if op != libublk::sys::UBLK_IO_OP_READ
        && op != libublk::sys::UBLK_IO_OP_WRITE
        && op != libublk::sys::UBLK_IO_OP_FLUSH
    {
        return Ok(-libc::EOPNOTSUPP);
    }
    let _admission = if op == libublk::sys::UBLK_IO_OP_FLUSH {
        Some(
            cancel
                .before_io(wake, shared.gate.acquire(Mode::Exclusive))
                .await?,
        )
    } else if op == libublk::sys::UBLK_IO_OP_WRITE {
        Some(
            cancel
                .before_io(wake, shared.gate.acquire(Mode::Shared))
                .await?,
        )
    } else {
        None
    };
    if op == libublk::sys::UBLK_IO_OP_FLUSH {
        for file in files {
            if let Err(e) = exact(
                opcode::Fsync::new(types::Fixed(file)).build(),
                0,
                "member FLUSH",
            )
            .await
            {
                shared.error.store(true, Ordering::Release);
                return Err(e);
            }
        }
        return Ok(0);
    }
    let bytes = u64::from(iod.nr_sectors) * 512;
    ensure!(
        bytes <= u64::from(max_bytes) && bytes <= i32::MAX as u64,
        "request exceeds validated buffer size"
    );
    let offset = iod
        .start_sector
        .checked_mul(512)
        .ok_or_else(|| anyhow::anyhow!("request offset overflow"))?;
    let parts = shared.geometry.split(offset, bytes)?;
    let mut tag = runtime.tag(index as usize)?;
    let mut first = 0;
    while first < parts.len() {
        let stripe = parts[first].stripe;
        let mut end = first + 1;
        while end < parts.len() && parts[end].stripe == stripe {
            end += 1
        }
        let permit = cancel
            .before_io(wake, shared.stripes.acquire(stripe))
            .await?
            .map_err(|e| anyhow::anyhow!("failed stripe {}", e.0))?;
        cancel.check_before_io()?;
        let group = &parts[first..end];
        if op == libublk::sys::UBLK_IO_OP_READ {
            for p in group {
                exact(
                    opcode::ReadFixed::new(
                        types::Fixed(files[p.data_disk as usize]),
                        p.request_offset as usize as *mut u8,
                        p.len as u32,
                        index,
                    )
                    .offset(p.backend_offset)
                    .build(),
                    p.len as u32,
                    "data read",
                )
                .await?;
            }
        } else if group.len() == 2
            && group
                .iter()
                .all(|p| p.len == CHUNK && p.backend_offset % CHUNK == 0)
        {
            full_stripe(shared, &permit, &mut tag, index, group, files).await?;
        } else {
            // Failure was rechecked by acquire; only now inspect the frontier. Entire
            // full repair -> all partial RMW phases retain this permit and admission.
            if stripe >= shared.first_unsynced() {
                repair(shared, &permit, &mut tag, files).await?;
            }
            for p in group {
                rmw(shared, &permit, &mut tag, index, p, files).await?;
            }
        }
        drop(permit);
        first = end;
    }
    Ok(bytes as i32)
}
/// # Safety
/// Same owned-future/original-CQE rule as request(); sync sources and arena are
/// independent of all normal queues. Rate waits occur before calling this.
pub async unsafe fn sync_stripe(
    shared: &Shared,
    runtime: &BpfRuntime<'_>,
    wake: &QueueWake,
    cancel: Cancellation,
    tag_index: usize,
    stripe: u64,
    files: [u32; 3],
) -> Result<bool> {
    let Some(_scanner) = shared.sync_control.begin() else {
        return Ok(false);
    };
    let _admission = cancel
        .before_io(wake, shared.gate.acquire(Mode::Shared))
        .await?;
    let permit = cancel
        .before_io(wake, shared.stripes.acquire(stripe))
        .await?
        .map_err(|e| anyhow::anyhow!("failed sync stripe {}", e.0))?;
    if stripe < shared.first_unsynced() {
        return Ok(true);
    }
    cancel.check_before_io()?;
    let mut tag = runtime.tag(tag_index)?;
    repair(shared, &permit, &mut tag, files).await.map(|_| true)
}

pub fn now_ns() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos()
        .min(u64::MAX as u128) as u64
}

/// Drain-owned finalization; FIFO exclusive gate follows all scanner admissions.
/// # Safety
/// Caller retains this task until every original fsync CQE is consumed.
pub async unsafe fn finalize_sync(
    shared: &Shared,
    wake: &QueueWake,
    cancel: Cancellation,
    files: [u32; 3],
) -> Result<()> {
    ensure!(shared.scan_done(), "finalization before complete frontier");
    shared
        .sync_finalization_started_ns
        .store(now_ns(), Ordering::Release);
    let _barrier = cancel
        .before_io(wake, shared.gate.acquire(Mode::Exclusive))
        .await?;
    for file in files {
        if let Err(e) = exact(
            opcode::Fsync::new(types::Fixed(file)).build(),
            0,
            "sync finalization FLUSH",
        )
        .await
        {
            shared.error.store(true, Ordering::Release);
            shared.request_stop();
            return Err(e);
        }
    }
    if !shared.stop.load(Ordering::Acquire) && !shared.error.load(Ordering::Acquire) {
        shared.sync_completed_ns.store(now_ns(), Ordering::Release);
        shared.sync_finalized.store(true, Ordering::Release);
    }
    Ok(())
}
