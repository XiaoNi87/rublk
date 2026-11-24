> Historical design and delivery plan. The prototype is implemented; the planned steps below are not an acceptance report. See [current publication status](RAID5_README.md). Local reference paths describe the original development environment.

# rublk RAID5 target design

Status: design-only development in `/home/xiao/rublk-raid5`, branch
`ublk-raid5`, independently cloned from `https://github.com/ublk-org/rublk.git`.
Implementation base: upstream `origin/main` at `c1178a43622b24e0fa491bd0f1b4a4c4bafa6614`.
No SMR implementation commits are inherited, merged or cherry-picked.

The design originated in `/home/xiao/rublk` on `ublk-smr` and incorporates
manual review responses through 2026-10-27. That repository is the user's
independent SMR workspace and is no longer the RAID5 development checkout.
The reviewer-owned `RAID5_DESIGN_REVIEW.md` remains there unchanged; it has
not been copied or edited as part of this workspace migration.

All `src/smr.rs`, `src/smr/queue_wake.rs`, SMR helper and ChunkWorkTable
references below are **external, read-only reference material** in
`/home/xiao/rublk`, not modules available in this checkout. Reuse their ideas
only after verifying the upstream interfaces; implement RAID5-local fixed-I/O,
queue wake and transaction helpers with their own tests. Do not depend on or
import the SMR branch to supply them. Step 1 must revalidate reference paths
and integration sites against this new upstream base before implementation.
The upstream manifest currently requests libublk 0.4.7 and io-uring 0.7.12;
step 2 still verifies the actual locked dependency graph and compatibility.

The numbered delivery plan is planned work, not test evidence. No RAID5
implementation is claimed here. Kernel demo/review reference sources remain
under `/home/xiao/linux-raid5-7.4/tools/testing/selftests/ublk/`.

## Goal and first release boundary

Implement `rublk add raid5` using the local kernel's `IORING_OP_BPF` extension
to calculate parity directly from ublk bio pages registered as KFIXED buffers.
The acceptance meaning of zero copy is specific: request data must not be
copied from kernel bio pages into a userspace request buffer. Old-data reads,
parity output, and BPF arena memory may still be userspace-addressable; their
existence must be reported separately in performance and copy accounting.

The first version supports exactly three equal-size conventional NVMe
block-device members, a fixed 64 KiB chunk, read, write, flush and
all-members-present crash recovery by full-capacity parity synchronization.
Regular files are supported only as explicitly identified test fixtures;
production admission rejects HDD/SATA and non-NVMe backends. Verify transport,
namespace identity, LBA geometry and cache/durability capabilities rather than
inferring NVMe from a path name. It requires
`--zero-copy` and the kernel BPF extension at device creation. It rejects
`--user-copy`, recovery with request reissue, discard, write-zeroes, FUA, and unsupported
backing-device geometry until their ordering and parity semantics are built.
Do not silently fall back to buffered ublk I/O or software XOR after promising
BPF zero copy. It exposes `(3 - 1) * usable_member_bytes` bytes, excluding the reserved
member metadata prefix defined below. Validate
queue depth, maximum I/O size, alignment, arithmetic overflow, distinct
backends, `max_io_buf_bytes >= 2*C` for the full-stripe path, and that each
backend is at least one chunk long before publishing
the device. Open backends with direct I/O where required by the fixed-I/O
path, reject duplicate `(device, inode)` identities and mounted/in-use block
devices, and reserve exclusive ownership for the target lifetime. Fresh
creation writes only its explicitly reserved metadata prefix after validation;
it never initializes or changes exposed data/parity regions.

### Fresh admission versus recovery

Fresh adoption of three unformatted backends still requires a bounded-memory,
read-only check of every exposed stripe (`P == D0 xor D1`), exact reads and
checked offsets; reject inconsistent inputs without modifying them. Do not
infer consistency from equal sizes or an operator assertion. An explicit
create operation reserves/writes metadata only after this check and ownership
validation; data-region bytes are not initialized or repaired by fresh add.
Never silently interpret arbitrary files as a recoverable dirty array.

Existing arrays are identified by valid member metadata, UUID, member order
and geometry. A clean, fully matching set may assemble without a full scan;
an unclean set with all members present starts full parity sync and may serve
normal I/O using the concurrency protocol below. Recovery may rewrite parity
in the data region; it is intentionally different from fresh read-only
admission. Exclusive backend ownership spans metadata validation through
teardown. Noncooperating external changes to owned members are unsupported.

No persistent dirty-region bitmap, llbitmap, PPL, data journal or persistent
sync cursor is used. This scope restores parity consistency from available
data with all original members accessible. Missing-member/degraded recovery,
replacement rebuild and atomic recovery of interrupted multi-block writes
are outside this version. The user explicitly accepts that full sync does
not provide write-hole protection for combined unclean-plus-missing-member
failure. Refuse missing/foreign/duplicate members; do not reconstruct from
potentially stale parity. Ordinary reads return available data, not a promise
that an interrupted application write was atomic. FLUSH/FUA durability remains
part of the block-device contract; reject FUA until its implementation exists.

## NVMe full-sync recovery protocol

### Member metadata and low-overhead dirty/clean lifecycle

Implement `src/raid5/metadata.rs` and `src/raid5/recovery.rs`. Reserve a
1 MiB metadata prefix per member, aligned to supported sector/chunk sizes;
validate this reservation before adoption. Two separate 4 KiB superblock
slots at offsets 0 and 4096 contain magic/version, checksum, array UUID,
member UUID/slot, full ordered member identity list, geometry, data offset,
capacity, generation and CLEAN/DIRTY state. Specify a fixed little-endian
encoding, zero reserved bytes, record length and CRC32C covering the entire
4 KiB slot with its checksum field zeroed; reject unsupported versions/lengths
and checked generation overflow. UUIDs/geometry are included in the checksum.
Fresh create requires the reserved prefix be empty/recognized unused storage;
unknown existing metadata is rejected, never silently overwritten.
Initially support validated 512 B
or 4 KiB logical sectors; reject unsupported metadata alignment. All layout
backend offsets become `data_offset + stripe*C + chunk_offset`, with checked
arithmetic. All capacity, verification, sync and backing-I/O paths exclude
metadata. Recovery JSON is a discovery hint, not authority over member records.
An additional immutable 4 KiB creation seal at offset 8192 on each member
contains checksum/version, array/member UUIDs, ordered identities and immutable
geometry. It is written only during first creation, after all initial slots
are durable, and never rewritten by DIRTY/CLEAN transitions. All three seals
must be valid and match before ordinary assembly or writable admission; see
the interrupted-creation protocol below. This distinguishes a complete array
from a partially provisioned identity set without trusting a userspace JSON.

Protocol requirements (record encoding, checksum and slot-write failure tests
must be specified before implementation is accepted):

1. Under exclusive ownership and with no old worker/ring I/O remaining,
   establish the takeover durability baseline below BEFORE selecting a durable
   survivor or accepting cached CLEAN records, then reread and validate
   geometry/identities from checksummed records. At least one valid
   compatible record per member is required; conflicting UUID/member order/
   format or untrusted geometry is an error, not an invitation to overwrite.
2. A clean fast start is allowed only when **all six slots** are valid and
   agree on a CLEAN generation/geometry and ordered membership. Otherwise,
   if identity/geometry is trustworthy, conservatively require full sync.
   Missing/torn/stale slots or mixed clean/dirty generations never authorize
   skipping sync. No atomic multi-device metadata-write assumption is made.
3. Before any request or sync can mutate members, write a new DIRTY generation
   into both slots on every member using the preserve-one-copy update below:
   exact first-slot write -> successful member flush -> exact second-slot
   write -> successful member flush. Never submit the second overwrite before
   the first flush has succeeded for that member.
   Do not admit mutations until all six DIRTY records are durable, eliminating
   residual older CLEAN slots as a false-clean source after a crash. Partial
   transition failure stops startup; preserve dirty/unknown state.
4. Keep this generation DIRTY for the entire writable session, including after
   background sync finishes. No per-write bitmap/metadata update is needed.
   CQE completion alone is not proof of persistence; use the validated NVMe
   flush/FUA path and test its error propagation. PLP does not imply cross-disk
   atomicity and is not substituted for the specified ordering.
5. Mark CLEAN only on orderly shutdown: close normal/sync admission, stop
   scheduling new work, drain/cancel and consume every original CQE, require
   full sync completed (or a clean baseline at startup), no failed stripes,
   and successful flush of all member data. Then write/flush the next CLEAN
   generation to both slots on all members using the SAME preserve-one-copy
   write/flush sequence. No subsequent data mutation is allowed; a partial
   CLEAN transition is not a successful clean shutdown.
   Errors, incomplete sync, partial writes or metadata failure forbid CLEAN.
6. Crash or recovery interruption leaves DIRTY/unknown; the next start scans
   from stripe zero. There is no durable checkpoint to skip already-scanned
   ranges. A full scan finishing is RUNNING, not permission to mark a live
   writable session CLEAN. Flush requests likewise never mark CLEAN.

### Takeover/retry durability baseline (required before slot overwrite)

A valid CRC after reread proves readable contents, not persistence. Process
exit does not clear NVMe volatile caches: a newly readable higher-generation
slot may still disappear on subsequent power loss. Never select a survivor
by generation or CRC alone at a restart/retry entry.

Use this mandatory entry sequence for assembly, retained-device recovery,
metadata-transition retry and create-resume. For simplicity run it at every
metadata transition entry; there is no per-request flush and no unproven
fast-path exemption for a seemingly clean restart.

1. Obtain exclusive member/metadata ownership, fence the old process/queue/
   ring and **all its metadata and backing operations**, and consume/drain
   their final effects. Freeze new mutations. Merely reading a new PID,
   obtaining a lock or observing a write CQE is not sufficient fencing.
2. Issue a baseline flush to each member only AFTER this quiescence and
   require successful completion from every member. Flushes may run across
   members concurrently. Before all succeed, submit NO slot overwrite or
   creation-seal repair. Do not count an old failed, cancelled or uncertain
   flush as proof, even when subsequent reads return valid records.
3. After baseline flush success, freshly read/validate both slots and seals
   on every member, using the direct-I/O/coherent path. With no intervening
   mutations, the readable valid records now have a durability baseline
   under the supported flush contract. Only then determine identity/CLEAN
   eligibility, choose survivor/target, and compute the next generation.
   Refuse incompatible or insufficient identity; flushing is not validation
   or repair and cannot manufacture a record when both slots are invalid.
4. The metadata owner tracks this baseline only for this quiescent transition.
   Any failed/uncertain subsequent write/flush, loss of ownership, or process
   restart invalidates it for retry; reenter at step 1, not at slot selection.
   A successful first-slot write+flush supplies the next survivor within that
   transition; baseline success alone does not permit unordered overwrites.
5. If baseline flush fails or its completion is uncertain, stop admission and
   metadata progression, drain issued work and report the error. Never cover
   the older slot to preserve a merely cache-visible newer one. Retry requires
   fencing and another successful baseline, not repeated reads alone.

This specifically handles durable A10 plus cache-visible B11: stabilize both
with the baseline flush before choosing to overwrite A10. If that flush fails,
leave A10 untouched. If it succeeds, B11 is now a valid durable survivor before
A10 can be overwritten. A restart after actual power loss follows the same
entry rule; do not assume every process restart also reset the controller.

### Preserve-one-valid-copy slot updates (required for established arrays)

A checksum detects a torn record but does not make a 4 KiB write power-fail
atomic. Do not assume either logical sector size or NVMe PLP provides the
necessary slot atomicity. Preserve one untouched durable identity record at
all times during updates, under the documented device flush guarantees.

1. With metadata transitions serialized and no competing metadata writer,
   first complete the takeover/retry durability baseline and fresh reread
   above. Only from that stabilized snapshot choose target T and an untouched
   survivor S whose compatible identity record is already durably valid.
   If only one slot is valid, it MUST be S; overwrite the invalid slot first.
   If both are valid, choose deterministically (older generation first, slot
   index as tie-break). Refuse established-array update if neither is valid.
   Choose the new generation above the maximum validated generation across
   all members/slots, with checked overflow; all member targets use that
   generation and compatible identities. In-memory validity from a prior
   failed write is not a durable survivor certificate.
2. Submit exactly one 4 KiB record write to T; require its exact CQE, then
   submit and successfully complete the flush for that member. Until this
   flush succeeds, S is untouched, no write to S is queued/linked, and T is
   not counted as durably valid even if its write CQE succeeded.
3. After T is durable, it becomes the survivor. Only now write the new record
   into S, check its exact CQE and flush that member successfully again.
   A crash tearing this second write still leaves T as a valid identity copy.
4. On any short/error/cancelled write or failed/uncertain flush, stop that
   member's progression immediately. Stop scheduling the overall transition,
   drain already issued operations on other members, retain the last confirmed
   survivor and report failure. Never attempt rollback by overwriting it.
   A later retry fences/drains old I/O, successfully baseline-flushes every
   member, then freshly rereads metadata before selecting any survivor or
   overwrite target. Mere reread/generation comparison is insufficient; no
   pending write is allowed to survive into that retry.
5. Members may execute these stages concurrently. Per-member dependency
   `write(T) -> flush -> write(S) -> flush` is mandatory; no global
   serialization of every member's metadata I/O is needed. Startup opens
   normal/sync mutation only after BOTH DIRTY slots on EVERY member are
   confirmed durable. CLEAN still additionally requires prior data drain/
   flush, no failed stripes, and stopped mutation as specified above.

This ordering allows mixed generations or one torn slot after interruption;
those are dirty/unknown and require full sync, never a false CLEAN fast path.
The survivor guarantees identity availability, not that its older CLEAN state
may authorize skipping sync. All-six-slot CLEAN matching remains mandatory.
The guarantee covers interrupted slot writes with working flush semantics,
not independent media corruption of untouched records or a lying device.

### First creation, completion seals and interrupted-create retry

First creation has no old valid survivor and is NOT an established-array
recovery case. Do not claim the above invariant before bootstrapping identity.

1. Lock all explicit members, validate empty/unused reserved prefixes and
   geometry, and complete the read-only data/parity adoption check. Allocate
   one array UUID and fixed member UUID/order for this creation attempt.
   No normal or sync writes and no writable device publication are permitted.
2. For each member, write the first initial DIRTY slot and flush it exactly;
   only after success write/flush the second initial DIRTY slot. Members can
   bootstrap in parallel. Before a member's first flush, a torn first write
   may leave no identity: report this as incomplete creation, not recovery of
   an established array. Never send both initial slot writes together.
3. After all six initial DIRTY slots are durably valid and agree, write and
   flush each member's immutable creation seal with those identities/geometry.
   Do not publish or mutate data until all three seals are confirmed durable.
   A seal has no fast-path update cost and no dependency on a future CLEAN
   transition. Established-array slot updates never overwrite its region.
4. Missing/torn/mismatched seals identify incomplete or invalid provisioning;
   normal assembly/recover rejects it, even if all visible slots are valid.
   Once every matching seal is valid, normal assembly can safely treat the
   provisioned set as an established DIRTY array: its data had been validated,
   and any interruption now simply requires full sync before clean shutdown.
5. An explicit create-resume operation may continue ONLY when every member has
   at least one trustworthy original identity record, the supplied UUID/order/
   geometry agrees with all valid records/seals, and exclusive ownership plus
   old-I/O fencing, successful all-member baseline flush, fresh identity reread
   and a fresh full read-only parity check succeed. A cache-visible valid slot
   or seal is not trusted as durable before the baseline. If baseline flush
   fails/has uncertain results, no slot or seal repair may be submitted.
   Repair slots using the
   survivor protocol, flush all initial records, then complete/retry seals.
   Never invent a new UUID for a partly created set or automatically adopt it.
   If original identity is unrecoverable, refuse resume; require explicit
   operator-directed metadata reinitialization as a separate destructive
   provisioning action, with data regions left untouched. Do not silently
   erase conflicting/unknown prefixes as part of recovery.

Test creation interruptions separately from established DIRTY/CLEAN updates.
A creation interrupted before durable identity/seals is allowed to require
explicit provisioning retry; an established array must retain at least one
valid identity slot per member across every allowed transition crash point.

Normal-write overhead is therefore the existing admission/stripe coordination,
not an extra dirty-metadata write per request. Recovery adds competing I/O and
per-stripe bookkeeping; measure those costs rather than claiming zero overhead.

### Recovery and ublk server restart entry points

`rublk recover` for a preserved device must recognize RAID5 JSON and enable
supported ublk user-recovery lifecycle, retaining geometry and tag/ring rules.
No automatic request reissue is supported: outstanding requests that cannot be
completed safely must fail through the kernel's validated recovery contract;
applications may retry. Step 4 proves actual fetch/commit/registration and
client outcomes; do not invent notification or reissue semantics. Add the
necessary dependency hooks if absent, and stop this gate if old I/O cannot be
fenced. Do not silently fall back to unsafe restart or require recovery flags
that were not enabled at original creation.

After reboot, assemble a new ublk device from the explicit original member
list and metadata (device numeric ID need not survive). Proposed CLI contract:
`rublk add raid5 --assemble <members>` for existing metadata, distinct from
fresh adoption, and `rublk recover <id>` for retained-kernel-device restart;
final clap argument spelling follows repository conventions. Both paths use
the same metadata/full-sync engine. Validate JSON against on-member identity
and capacity. Freeze old delivery and prove old queue/ring/backing I/O is gone
before reacquiring writable ownership; an expired PID or dropped result waiter
alone is not a fence. Add kernel/dependency lifecycle changes if required by
the spike, with review and explicit tests before data-path implementation.

Expose states STARTING, RECOVERING, RUNNING, STOPPING and ERROR with UUID,
session generation and progress. Readiness in RECOVERING means reads and writes
are allowed under the protocol below, not that parity is already consistent
everywhere. Startup is blocked until metadata, ring/BPF and worker resources
are ready; failed startup never publishes a writable half-device.

### Background sync worker, progress and resources

For each stripe, ignore old parity, read the two mapped data chunks into two
aligned USER slots, compute full `C` parity with BPF USER/USER inputs into its
arena slot, then ordinary-Write that parity chunk. Exact reads, BPF length and
write CQE are mandatory. Sync never rewrites data chunks. The additional
USER/USER operation and sync ring attachment must pass steps 5–6.

Use one dedicated recovery thread/ring with fixed backend fds, BPF attachment,
executor/transaction registry and independent three-slot buffers. Start with
a bounded window of 32 stripe transactions (validated/configurable for tests),
one in-flight permit per stripe transaction, no reservation of the entire
window's stripe permits in advance. Progress scheduling is increasing stripe
ID. Different stripes can execute concurrently; their results may complete
out of order. Recovery slots and ring indices never alias normal request tags
or normal arenas; budget `window * 3 * aligned(C)` plus metadata/descriptor
and bounded completion bookkeeping separately from normal queues.

Maintain a volatile atomic contiguous frontier `F`: every stripe `< F` has
completed valid baseline sync in this session. Use a bounded coordinator
completion set for out-of-order work within `[F, min(F+window, nr_stripes))`;
do not schedule beyond that range even when a later slot completes first.
This bounds bookkeeping behind a delayed head and prevents an unbounded set
of completed stripes. Advance F
only across consecutive exact successes, using release/acquire publication.
No persistent or full-capacity per-stripe bitmap is allocated. F is a runtime
consistency certificate, **not a durability checkpoint**. Normal transactions
preserve consistency or publish failed before releasing the same permit;
failed checks always take precedence over F. On-demand repair above F is not
recorded in a global completed set; the scanner still visits it. On restart
F resets to zero, regardless of prior displayed progress.

After F reaches the last exposed stripe, stop new sync admission, drain its
registry, take exclusive flush admission with no stripe permit held and flush
all members. Only success with no failed stripe permits RECOVERING -> RUNNING.
Normal writers may resume after the barrier; metadata stays DIRTY. Failure
leaves ERROR/dirty, never a completed/clean state. Flush success during an
incomplete scan does not mean recovery completion.

### Sync versus normal I/O: one ownership protocol

All sync and normal transactions use the **same** sharded StripeWorkTable,
failed-state recheck and FIFO permit for a stripe. A separate sync mutex would
not protect against normal I/O. Keep the existing first-release exclusive
permit for reads as well. No backend access may race an owner of that stripe.

| Operation in RECOVERING | Behavior while holding the stripe permit |
| --- | --- |
| Normal read | Recheck failed, ReadFixed the mapped data; parity is not needed. It can proceed above F without waiting for the full scan. |
| Partial write/RMW to stripe `< F` | Recheck failed and use the ordinary validated RMW path. |
| Partial write/RMW to stripe `>= F` | Recheck failed, read both full data chunks and BPF-rebuild/write full parity first, consume all repair CQEs, then read old data/parity and execute RMW **without releasing the permit or write admission between repair and update**. Never use untrusted old parity. |
| Complete aligned full-stripe write | Recheck failed; write all new data plus new parity under the permit. It does not rely on old parity and need not pre-repair. It does not advance F. |
| Background sync | Shared flush admission first, then permit/recheck failed; read current data, compute/write parity and consume all CQEs before permit release. Never pre-read data outside the permit. |
| FLUSH | Exclusive flush admission only; drain earlier normal writes and admitted sync/repair writes, flush all members; do not acquire stripe permits or mark the scan complete. |

A request containing any partial-stripe segment applies the repair rule per
segment after grant; offsets/length alone do not allow an incomplete stripe
to use the full-stripe bypass. A full-chunk write is still a partial-stripe
write. On-demand repair uses the requesting tag's three slots in sequence,
reusing them for RMW only after repair CQEs are consumed; keep new request
pages in KFIXED throughout. No request bio data is copied into USER storage.

The above deliberately trades extra full-stripe reads/parity writes for no
bitmap: repeated partial writes above F may repeat repair until the scanner
passes. After catching up, normal RMW returns to its original cost. A future
cache of repaired ranges needs a separately bounded design and tests, and is
not silently introduced here. Baseline parity repair or sync failure poisons
the stripe; automatic sync cannot clear the lifetime failed set or resurrect
it. A recovery read error leaves data untrusted and must poison/stop recovery
rather than computing from short/unknown bytes. Unexpected sync/repair I/O
failure stops new write/sync admission device-wide after publishing failure;
drain owners, report ERROR and retain DIRTY. Existing reads may finish but
must observe failed checks; further use requires explicit restart/diagnosis.

Race proofs to test:

1. Sync wins the permit: normal writes wait; after exact sync completion their
   RMW sees the resulting parity (or conservatively pre-repairs if still >= F).
2. Normal RMW wins above F: its repair and update are one ownership interval;
   scanner later reads the new data and cannot overwrite parity from old data.
3. Normal full-stripe write wins: scanner later derives parity from that new
   stripe, not data prefetched before acquiring its permit.
4. F advances while a task waits: re-read F only after permit grant; either
   path is safe. A stale smaller F only causes redundant repair. Never let
   out-of-order completion advance F past a hole or a failed stripe.

### Lock order, fairness, cancellation and stop during recovery

Use the existing order for every mutator, including background sync:
`shared flush admission -> one stripe permit -> failed/F check -> I/O drain
-> failed publication if necessary -> permit release -> admission release`.
Normal requests retain shared admission across all segments. Reads omit gate;
FLUSH/recovery-finalization take exclusive gate with **no** stripe permit.
Never hold a permit while awaiting scan progress, another permit or exclusive
admission. Coordinator/frontier locks are not held during gate/shard acquisition
or `.await`; completion reporting and wake callbacks occur after bookkeeping
locks are released. Atomic F publication does not replace failed-set locking.

Sync cancellation follows the independent cleanup-owner rules exactly:
stop new scheduling, retain permit/admission/storage until every original CQE
(and any cancel CQE) is consumed, publish failed for incomplete/uncertain
parity writes before handoff, and never advance progress for cancelled/failed
work. Cancellation before a parity write can leave the stripe simply unscanned;
controlled stop remains DIRTY if any scan is unfinished. Full-sync restart
never trusts volatile progress, and no resource is freed early to meet a timeout.

Bound sync submission by both window and a rate budget. Rate limiting/pausing
occurs **before** flush admission and stripe enqueue, never while holding a
permit. The shared FIFO prevents queued FLUSH starvation; per-stripe FIFO
prevents sync starvation by later normal requests under eventual completion.
Bounded sync admission prevents an unbounded batch of sync holders delaying
FLUSH. Measure normal-I/O p99 and FLUSH tails under sync, tune the window/rate,
and expose pause/resume/status; pausing retains RECOVERING/DIRTY and on-demand
repair rules. Shutdown first stops both producers, drains both registries and
then applies the metadata clean decision. Do not join a worker while holding
locks it needs to drain. No normal operation may bypass these rules merely
because the device is in RECOVERING rather than RUNNING.

## Layout contract

Use one pure, tested function for every read and write path. With `C = 64 KiB`,
logical byte offset `L`, and `N` backends (`N = 3` enforced at the CLI):

```
data_disks   = N - 1
stripe_bytes = checked_mul(data_disks, C)
stripe       = L / stripe_bytes
data_index   = (L % stripe_bytes) / C
chunk_offset = L % C
parity_disk  = N - 1 - (stripe % N)
data_disk    = (parity_disk + 1 + data_index) % N
backend_off  = checked_add(data_offset, checked_add(stripe*C, chunk_offset))
```

This is left-symmetric rotation. For stripe 0 the physical order is
`D0,D1,P`; for stripe 1 it is `D1,P,D0`; for stripe 2 it is `P,D0,D1`.
Construct full-stripe write descriptors in **logical data-index order**,
then obtain each backend from this function. The Linux demo's disk-order
full-stripe writer disagreed with its read mapping on rotated stripes.
Requests crossing a chunk or stripe are split into bounded segments, and
their KFIXED offsets are relative to the original request. Reject requests
outside capacity or with unsupported sector alignment before submitting any
backing operation.

## rublk integration

- Add `src/raid5.rs` with `Raid5AddArgs`, setup, target state, queue handler,
  and a target JSON schema/version for inspection. Register `Raid5` in
  `src/args.rs` and the add/recover dispatch in `src/main.rs`; the recover arm
  enters the validated user-recovery/assembly lifecycle above. Update the zero-copy whitelist in
  `GenAddArgs::new_ublk_ctrl`, `validate_target_compatibility`, module declaration,
  `ublk_parse_add_args`, `ublk_add_worker`, and `ublk_recover_work`. Persist a
  recognizable `raid5` target name and versioned configuration (layout, chunk,
  backend identities, capacity and data offset) for recovery validation. Keep the
  target separate from `smr.rs`.
- Follow `src/zoned.rs` for `run_target` and per-tag async lifecycle. Follow
  `src/smr.rs` for a bounded per-queue `LocalExecutor` drain and queue wake
  bridging. Follow `src/loop.rs` and SMR fixed-I/O helpers for fixed-file
  `ReadFixed`/`WriteFixed` SQEs and exact CQE byte checks. Zoned's
  `UBLK_F_USER_COPY` data path is not a ZC reference.
- Set `UBLK_F_SUPPORT_ZERO_COPY | UBLK_F_AUTO_BUF_REG` only after probing
  required features. Each tag uses `BufDesc::AutoReg` with `index = tag` and
  **no** `UBLK_AUTO_BUF_REG_FALLBACK` flag. The current loop/SMR helpers set
  that fallback flag, so RAID5 needs its own descriptor constructor. For
  this pinned kernel, successful normal delivery of a data request with this
  descriptor implies auto-registration succeeded; a failed registration is
  ended by the kernel and is not normally delivered to the target. Verify
  that dispatch contract and tag/fetch lifecycle in step 4, not a fictional
  userspace per-failure notification.
- Each queue owns its io_uring, registered backing fds, BPF attachment, and
  arena allocation. A KFIXED index belongs to that queue's ring; never move
  an in-flight tag or BPF source descriptor to another queue. The queue-owned
  transaction task retains request registration and descriptors until every BPF and
  backing CQE is consumed, then commits exactly once through libublk.

### Dependency and ring ownership contract (R1, R6)

The installed `io-uring = 0.7.12` has no BPF opcode/setup flag/UAPI descriptors;
`libublk = 0.4.7` hard-codes ring builder flags. The local libublk fork has no
BPF implementation. This is a dependency integration project, not an existing
API that the target can simply call.

Use dedicated, pinned forks of **both** libublk and io-uring as the initial
plan. Add the typed extension/UAPI and setup flag to io-uring; add an explicit
opt-in queue-ring setup configuration to libublk. Keep ordinary targets on
unchanged default behavior. Resolve every transitive io-uring reference to
one compatible fork through Cargo configuration and verify with `cargo tree`.
Never edit Cargo registry caches. Record fork URLs, exact commits, patches,
build dependencies, BPF compiler/libbpf versions, and lockfile in the spike
report. If distributable fork URLs are not available, vendor these two crates
under a documented repository directory with provenance and licenses; do not
leave absolute `/home/xiao` path dependencies as the delivered solution.
Finalize fork versus vendor packaging at the end of step 2, before target work.

BPF tooling baseline: write the BPF program in C, compile with clang's BPF
target, and use libbpf through Rust `libbpf-rs`/`libbpf-cargo`. A feature-gated
`build.rs` uses `libbpf-cargo` to generate the Rust skeleton into `OUT_DIR`;
the Rust loader embeds that generated object/skeleton. Do not assume bpftool
generates `skeleton.rs`, and do not introduce Aya as a second loader. Step 2
must pin mutually compatible crate/libbpf/clang versions and prove that this
loader supports the local struct-ops extension. If it cannot, document and
validate a small C/libbpf FFI loader instead before changing the baseline.
Record BTF/vmlinux.h provenance, include paths, rebuild inputs and clean-build
commands; ordinary builds without the RAID5 feature must not invoke clang.

Pin extension ABI checks to kernel commit
`42abd4d4c48750e82cb24f52d23ab5c07b2d81c9` in
`/home/xiao/linux-raid5-7.4`. Cross-check opcode values, flags, descriptor
size/alignment/field offsets, reserved fields and struct-ops attachment against
that tree with a C layout probe and Rust compile-time checks. Unsupported ABI
must fail startup; there is no upstream-kernel compatibility claim.

Verified correction to review R6: `QUEUE_RING` in installed libublk `io.rs`
is declared inside `std::thread_local!`, not a process-global shared ring.
It is nevertheless initialized once **per thread**. Require one dedicated OS
thread per RAID5 queue, one ring initialization with BPF flags before any
ordinary queue helper initializes it, and one queue/BPF/arena lifetime on that
thread. Reject incompatible pre-existing ring configuration. Do not place two
RAID5 queues on the same thread. Prove two simultaneous queue threads obtain
separate rings and correct per-ring BPF attachments; inspect actual attach
semantics rather than inferring them from thread locality. If this cannot be
proved, stop and revise the ownership/API design before data-path work.

The compatibility gate includes a BPF SQE through
`UblkQueue::ublk_submit_sqe`, exact CQE checking, two-source KFIXED parity,
three-source mixed USER/USER/KFIXED RMW, kernel-only auto-reg failure behavior, and
multi-queue attachment/teardown. A single successful BPF invocation does not
close R1 or R3. The numbered plan defines the evidence required, including the arena-to-backend
parity-write gate in step 5.

## Parity and I/O transactions

Use a new BPF XOR program with two-source full-stripe and three-source RMW
operations. The BPF descriptor carries source type, registered index, source
offset/length, parity destination offset, and requested length. Validate all
fields before constructing iterators. Each iterator creation and page walk
must succeed; return an error for any short result. Bound the loop by the
validated maximum chunk length and reject unsupported lengths. The parity
arena is queue-local, aligned, and divided into nonoverlapping tag slots;
budget old data, old parity, and new parity explicitly (R2):

```
C                 = 64 KiB
A                 = verified backing/BPF buffer alignment
slot_bytes        = checked_align_up(C, A)
tag_bytes         = checked_mul(3, slot_bytes)
queue_arena_bytes = checked_mul(queue_depth, tag_bytes)
old_data(tag)     = checked_mul(tag, tag_bytes)
old_parity(tag)   = checked_add(old_data(tag), slot_bytes)
new_parity(tag)   = checked_add(old_parity(tag), slot_bytes)
```

Each tag executes at most one chunk transaction at a time. Full-stripe uses
only its new-parity slot; RMW uses all three. Requests spanning chunks reuse
slots only after all prior transaction CQEs are consumed. Include BPF arena
metadata/alignment overhead separately using the actual allocation API;
validate total memory across all queues and memlock/resource limits before
publishing the device. Every descriptor range must be within its assigned
slot and allocation. No fixed 64 MiB allocation or unchecked offset arithmetic.

RMW old-data and old-parity inputs use ordinary aligned userspace storage
with `IO_BPF_BUF_USER`; they do **not** require prior io_uring buffer
registration. In the pinned kernel, `io_bpf_import_buffer()` in `bpf_ext.c`
imports USER via `import_ubuf()` rather than consulting the registered-buffer
table. Iterator advancement uses `iov_iter_extract_pages()` and the iterator
unmap/destroy path releases the pins it acquires. Retain valid, accessible,
stable user mappings and slot contents across backing reads and BPF completion;
never unmap, resize or reuse them while a referencing operation is outstanding.
Address-range import alone is not proof that all pages can be extracted: test
page-walk errors and exact-length reporting. `mlock` is neither a required
registration API nor proof of iterator/direct-I/O compatibility.

The spike establishes whether the chosen arena mapping can also serve as
USER read-back storage. If it cannot, use two separately aligned ordinary
USER input slots plus the arena output slot under the same three-slot budget.
Only if explicitly choosing a pre-registered input alternative should the BPF
descriptor become `IO_BPF_BUF_FIXED`, with the registered index and an absolute
address within that buffer (unlike KFIXED's relative offset). That alternative
needs separate FIXED/FIXED/KFIXED and registration-lifetime verification;
registering storage while still using USER does not select fixed-buffer lookup.
All input/output allocations, mappings, descriptors and any optional fixed
registrations stay owned until every referencing operation's CQE is consumed;
only then may cleanup unregister/free them.

### Three memory classes and backing SQEs (parity-write gate)

A registered backing **file** (`IOSQE_FIXED_FILE`) does not make its source
buffer fixed. Keep file-table indices, ublk KFIXED indices and separately
registered USER-buffer indices distinct and validate any shared index-space
reservation in the dependency spike.

| Memory class | Backing SQE baseline | Registration and lifetime requirement |
| --- | --- | --- |
| Request data: ublk auto-registered KFIXED pages | `ReadFixed` / `WriteFixed` with the request tag index and original-request byte offset | Auto-reg success is mandatory; retain until every referencing CQE is consumed. Never substitute an arena pointer for a KFIXED offset. |
| New parity: BPF arena user mapping | Ordinary `Write` using the mapped output-slot user address, direct-I/O aligned length and backend offset | The actual arena VMA/pages must support the kernel's direct-I/O pin/import path. BPF completion precedes submission; retain mapping/storage through write CQE. Ordinary `Write` does not require prior io_uring fixed-buffer registration, but pinability must be proved. |
| Old data/parity: two aligned USER input slots | Ordinary `Read` into each user address initially | Prove direct-I/O pinability and BPF USER page extraction from valid accessible addresses. No io_uring pre-registration is required for USER. Retain mappings/slots through BPF and all referencing CQEs; iterator-owned pins are released by the kernel. |

This baseline intentionally does not use `WriteFixed` for parity or
`ReadFixed` for old-data/parity. The demo (`raid5.c:1801,1824`) likewise uses
fixed KFIXED data writes and an ordinary user-address parity write; this is
reference evidence, not proof that our BPF arena mapping supports that path.
If ordinary direct-I/O on the actual arena mapping fails, stop the spike.
An alternative is explicit io_uring buffer registration and fixed backing I/O
with its own registered index and real user address/offset semantics; if those
registered inputs are used as fixed BPF sources, choose `IO_BPF_BUF_FIXED`; prove
registration support, noncollision with tag indices, alignment and lifetime
before adopting it. Do not assume registration makes an unpinnable VMA usable.
Do not silently copy parity into a bounce buffer to bypass this gate; any such
change requires an explicit revised memory/copy-accounting design.

Step 5 must prove **both** USER input reads and arena output writes through
libublk's actual SQE/CQE path with O_DIRECT backends, including nonzero slot
offsets. Neither a successful BPF XOR nor successful USER input reads alone
close this gate. Confirm completed BPF writes are visible to backing
I/O and define any required synchronization from the actual kernel contract.

| Operation | Sequence while owning the stripe | Completion condition |
| --- | --- | --- |
| Read | Map each data segment, issue `ReadFixed` into the request's KFIXED offset. | Every CQE reports its exact requested length; otherwise return I/O error. |
| Full-stripe write | BPF XOR the two logical KFIXED data chunks into the tag's parity slot. After checking its CQE, issue two `WriteFixed` data writes and one ordinary `Write` from the mapped parity-slot user address, potentially in parallel. | BPF result is exactly `C`, and all three writes report exactly `C`. |
| Partial write / RMW | Read old data and old parity into separate aligned tag-local buffers. BPF computes `newP = oldP xor oldD xor newD`, where `newD` is the request's KFIXED segment; then `WriteFixed` new data from KFIXED and ordinary `Write` new parity from the mapped arena address. Split a request covering two data chunks into bounded per-chunk RMW transactions. | Both reads, BPF, and both writes report exact lengths. |
| FLUSH | Stop admission of later writes, wait for prior admitted writes, issue `Fsync` to all three backends, then release the barrier. | All three sync CQEs succeed; report the first error otherwise. |

For a partial write, the three BPF inputs may use mixed iterator types:
userspace-addressable old-data/parity buffers and KFIXED new data. The BPF
spike must prove this combination and exact-length reporting on the actual
kernel. No chained write with `IOSQE_CQE_SKIP_SUCCESS` may start before the
BPF byte count is checked. Concurrent data/parity writes can partially
complete; on any write failure, mark that stripe failed in memory, reject
further I/O to it, return `-EIO`, and require operator inspection. This is
runtime failure containment. Explicit all-member restart can rebuild parity
from readable data via full sync; it cannot correct unknown data corruption.
Journal/CoW write-hole protection is outside the requested recovery scope.

## Locking, ordering, and shutdown

### Sharded stripe management and session-lifetime failed state

Implement standalone `src/raid5/stripe_work.rs` with deterministic unit tests
and property/model-based state-machine tests. Start with 64 shards, fixed for
the device lifetime; benchmark internal configurations 1/16/64/256 before
considering another default. Validate nonzero shard count and checked sizing.
Use a documented deterministic integer mixing hash, with
`shard = hash(stripe_id) % shard_count`, so sequential and patterned offsets
are distributed; a stripe always maps to the same shard on every queue.

Each shard has one short `std::sync::Mutex` protecting:

- transient stripe entries: owner/grant state plus FIFO waiters;
- a separate monotonic failed-stripe set retained until device teardown.

No device-global stripe-map mutex or separate failed-set mutex is used.
Reclaiming an empty transient entry must never remove its failed record.
The same shard mutex serializes failed publication, grant bookkeeping and
failed recheck, providing visibility across queue threads without a separate
atomic/cache protocol. A failed record includes enough reason information
for diagnosis, but logging/formatting occurs outside the lock. Failure
publication must not silently fail on allocation: reserve a failure marker
before any backing write (for example in a retained stripe status record),
or poison the whole device using preallocated state if recording failure
cannot be guaranteed. Never release a possibly damaged stripe as healthy.

A permit is an **asynchronous exclusive ownership right**, not a thread mutex
held while I/O runs. Each waiter has a unique token and registers its listener
before enqueueing. Under the shard mutex, enqueue, cancel, grant or release
updates bookkeeping and selects successor notification handles. Unlock before
notifying listeners, waking executors, invoking callbacks, logging, or dropping
objects whose destructors may reenter/acquire locks. The listener/token state
machine must handle a grant followed by cancellation before notification;
a late notification is harmless and cannot create a second owner. Never hold
a shard mutex across `.await`, SQE submission, BPF/backing I/O or CQE wait.
Queue threads may briefly block on bookkeeping mutex acquisition, so measure
that contention; sharding reduces rather than proves its absence.

A queued guard may remove its token on cancellation; a head grant with no
submitted I/O may hand off directly. Once obtained, the permit belongs to the
independent transaction owner below. Permit Drop only performs bookkeeping
and schedules notifications after unlocking; asynchronous I/O cleanup must
already be complete. Model enqueue/grant/cancel/release/shutdown races and
assert one owner, FIFO among live waiters, no lost wake/double grant/release,
eventual handoff and cleanup. Do not directly copy SMR's waiter implementation.
QueueWake only bridges runnable tasks to their queue, not data ownership.

### Failed-state recheck and lock order (mandatory correctness contract)

An optional pre-enqueue failed check is only an optimization. **Every normal read/write segment and background sync transaction must, after acquiring its stripe permit and before submitting
any backend/BPF SQE, lock that stripe's shard and recheck persistent failed
state.** If failed, submit no I/O and complete that segment with `-EIO`.
The recheck is performed under the same shard mutex as publication. The
permit remains held after the mutex is released; therefore another owner
cannot mutate the stripe between the check and submission. Publish failure
under that mutex after all owned I/O drains and before releasing the permit;
select/wake a successor only after failure is visible. A waiter that checked
healthy before enqueue may thus never continue on subsequently failed parity.

Use this fixed order for writes:

```
async shared flush admission (request-wide)
  -> briefly lock owning shard, enqueue, unlock
  -> await stripe permit without holding any thread mutex
  -> briefly lock same shard, recheck failed, unlock
  -> submit and drain this segment's I/O while retaining permit/admission
  -> if needed, briefly lock same shard, publish failed, unlock
  -> release stripe permit via short shard bookkeeping; unlock before notify
  -> after all request segments finish/drain, release flush admission
```

Background sync/repair uses the write lock order and shared admission too.
Reads omit flush admission but use the same permit, failed recheck and drain
rules. FLUSH takes only exclusive admission and never a stripe permit.
Never hold one stripe permit while awaiting another, never hold two shard
mutexes, and never acquire flush admission while holding a stripe permit or
shard mutex. Gate bookkeeping mutex and shard mutex are never nested; neither
calls notification callbacks while locked. Registry operations run on their
own queue thread without a cross-thread mutex; cross-queue wake is posted
only after bookkeeping locks have been released. Backend ownership is acquired
before startup verification and retained through teardown, with no hot-path
reacquisition or inverted lock dependency.

Initially all reads and writes of one stripe use the exclusive FIFO. Thus a
4 KiB read serializes other reads of the entire 128 KiB logical stripe even
when they access different disks. This is an explicit first-release
correctness/performance tradeoff, not a claim of a final high-concurrency
scheme. Measure same-stripe read hotspots and disjoint-stripe traffic before
considering fair per-stripe reader/writer permits: concurrent readers, exclusive
writers, and no later-reader bypass of queued writers. Writes remain exclusive
across the whole stripe because different data chunks may update the same
parity bytes. Process multi-stripe requests in increasing logical order, hold
at most one stripe permit and retain request registration through all segments.

### Transaction cancellation and cleanup ownership (follow-up P1-2)

A queue-owned transaction registry retains each transaction task independently
of the caller awaiting its result. That task owns the request/tag registration,
stripe permit, write-side flush admission, buffers, descriptors and pending-SQE
accounting. Dropping the caller or signalling queue shutdown requests
cancellation; it must not abort/drop the transaction task or its guards. The
queue continues driving these tasks/CQEs until cleanup finishes. No early ublk
commit/tag reuse is allowed. Rust synchronous Drop only signals cancellation;
it cannot await CQEs or serve as the asynchronous cleanup owner.

Use these ordered cancellation states:

1. Before obtaining a stripe permit, remove the queued waiter token (including
   a head grant with no I/O submitted); release any write admission and finish.
2. After obtaining a permit but before submitting any SQE, cancellation may
   release the permit/admission directly because no I/O can outlive them.
3. After any SQE submission, stop scheduling later transaction phases. Retain
   **both stripe permit and write flush admission**, plus all referenced
   storage/registration. Await issued operations, or submit supported cancels
   and consume both cancel CQEs and all original-operation CQEs; a successful
   cancel CQE alone does not discharge original completion accounting.
4. After all original results are consumed, mark the stripe failed before
   releasing its permit if any write was submitted and the complete intended
   data/parity update cannot be proved successful. Cancellation before writes
   requires draining reads/BPF but does not itself poison the stripe. A fully
   completed, validated write need not be poisoned solely because its caller
   disappeared. Preserve a terminal result and exactly-once commit accounting.
5. Only after result validation/failed-stripe publication and I/O drain release
   the stripe permit, then write admission, and finish completion/cleanup.
   Retain request registration and tag slots through any remaining request
   segments and commit references; never reuse/free before their final CQE.

For a multi-stripe request, retain request-wide write admission until every
submitted segment drains; hold at most one stripe permit and release it only
after that segment drains. Cancellation stops later segments. An independently
owned FLUSH task likewise keeps exclusive admission through all submitted
fsync CQEs. Queue teardown stops admission, requests cancellation, runs the
registry to empty, and only then destroys its executor/ring/attachments.
If completion cannot be established, fail/halt teardown with resources and
ownership retained rather than hand the stripe to a successor or free live
storage. There is no timeout-based early permit release.

### Concrete fair asynchronous flush gate

Implement target-local `src/raid5/flush_gate.rs` as an explicit FIFO admission
state machine, rather than depending on unspecified async RwLock fairness.
A short bookkeeping mutex protects a token FIFO, active shared count and an
exclusive-owner flag. Waiting uses registered listeners and yields the queue
thread; a blocking OS read/write lock must not cover admission waiting or I/O.
The bookkeeping mutex never nests with a stripe shard mutex.

1. Every normal write, sync transaction or FLUSH atomically appends a unique admission token as shared (normal/sync write) or exclusive (FLUSH) respectively. This enqueue is the cross-queue admission-order
   linearization point; do not claim an order among concurrently submitted
   requests before that point. Define and test queue fetch-to-admission
   sequencing so earlier admitted writes precede the FLUSH.
2. With no active exclusive owner, grant only the FIFO's leading contiguous
   shared group. Later shared arrivals may join only if no exclusive token
   precedes them. Shared permits coexist; this gate does not serialize writes.
3. A head exclusive token is granted only after all earlier shared owners
   drain and no other exclusive owner is active. While it is queued/active,
   later writers cannot bypass it. It holds admission through all three fsync
   CQEs, including cancellation/error cleanup.
4. Cancellation removes a pending token or releases a granted-but-unused
   permit and advances eligible successors. After I/O submission only the
   independent transaction owner may release it, after drain. Under the gate
   mutex choose grants/update state; unlock before notifications/callbacks.
   Register listeners before enqueue and test grant/cancel/notify races.
5. Normal-write and background-sync admission is acquired **before stripe enqueue**, held request-wide
   through all segments and cleanup, then released. Do not move it behind
   stripe acquisition to reduce FLUSH latency: that changes ordering and risks
   lock inversion. Hot-stripe writes already admitted intentionally delay a
   following FLUSH, even if they have not acquired their stripe permit yet.
   Sync has a bounded admission window and never holds a stripe while
   requesting exclusive finalization; rate-limit sleeps happen before admission.

Prove FIFO fairness under eventual completion of owners, cancellation and
continuous later arrivals. A stalled owner can still delay FLUSH; fairness
cannot make pending I/O finish. Queue shutdown follows the transaction drain
contract; failing a request does not discharge original CQEs. Destroy gate,
ring and BPF resources only after their owners have drained.

## Performance and observability

Start with the async completion graph above; keep per-tag allocation and BPF
attachment out of the hot path. Submit independent old-data/parity reads
together and independent data/parity writes together. Batch SQEs where the
libublk queue API allows it, but retain the BPF-CQE validation boundary.
Avoid a global mutex over backing I/O, a synchronous CQE wait inside a queue
callback, and per-completion eventfd syscalls. Do not assume the stripe FIFO
improves throughput: `SMR_DESIGN.md` reports mixed results for its chunk FIFO.

Expose counters/timings for request type, BPF calls and byte counts, KFIXED
registration capability failures and delivered KFIXED/BPF errors, copy
fallback attempts visible to userspace (must stay zero), RMW read/BPF/
write phases, stripe queue depth/wait, per-shard mutex acquisition wait/hold and occupancy,
flush admission wait/bookkeeping cost, flush p50/p99/max, short CQEs, and failed
stripes. Record queue depth, maximum I/O, backend type, and BPF program
revision alongside benchmarks. Compare 4 KiB RMW and 128 KiB full-stripe
workloads at queue depths 1/16/128, same-stripe and disjoint-stripe access,
one and multiple queues, throughput and p50/p99 latency. Attribute observed
costs before changing locking or adding hard-linked SQE chains.

Auto-reg runtime failures may be kernel-only and are not a target counter.
For this prototype accept the kernel's per-request `BLK_STS_IOERR` with fallback
disabled; the target continues serving other successfully delivered requests.
`ublk_auto_buf_register()` ends a failed request; `ublk_auto_buf_dispatch()`
does not issue normal `io_uring_cmd_done()` delivery for `AUTO_BUF_REG_FAIL`.
Do not manufacture a target failure CQE/commit for that undelivered request or
claim it triggers device-wide shutdown. External test-side kernel tracing and
client I/O errors establish the failure and absence of fallback. All such
counts must be labelled kernel-observed, not target-received. A future
all-device shutdown policy would first require an explicit kernel notification
ABI and separate design/testing; a libublk wrapper alone cannot provide it.

## Required correctness, md comparison and coverage evidence

### Independent data and physical parity correctness

Deliver a reusable deterministic verifier and machine-readable correctness
matrix, not only successful fio exits or self-readback of one data pattern.
The verifier must compute expected layout/parity independently of the target's
mapping/XOR helpers. Record seed, offset, length, sequence/version and checksum
in test payloads; compare complete payload bytes, not a constant zero pattern.

1. Exercise sequential/random reads, full-stripe writes, 4 KiB RMW, variable
   aligned lengths, all three rotations, first/last exposed sectors, nonzero
   request offsets and cross-chunk/stripe requests. Keep untouched-range
   sentinels and metadata-prefix canaries; respect deliberately mutable slots.
2. After writers and sync are stopped/drained, read every exposed logical
   block of each functional fixture and compare against an independent
   expected-content model, then directly read all three backing members and
   verify mapped data positions and `P == D0 xor D1` on every exposed stripe.
   Parity verification must not race normal/sync writes. Quiesce/release
   ownership before opening members externally; do not bypass live ownership.
3. Use fio verify/checksum workloads as an additional integration check, with
   non-overlapping regions per job when deterministic final-content comparison
   is required. For overlapping writes use controlled barrier schedules and
   a bounded per-stripe operation-history oracle checking allowed serialized
   outcomes; arbitrary last-completion order is not a valid last-writer oracle.
   Define expectations per supported atomicity/segmentation boundary, not a
   fictional multi-stripe transaction. Random stress also checks payload
   integrity, untouched ranges and physical parity after quiescence.
4. During recovery race sync against reads/RMW/full writes, force both permit
   ownership orders, holes/frontier changes and repeated on-demand repair.
   After stable completion require full logical and physical verification;
   deliberately stale parity must be corrected without rewriting data chunks.
5. For crash tests keep a durable expected-data log outside the failure domain
   (host-side for a guest power cut). Verify successfully flushed epochs and
   immutable anchor ranges not subsequently overwritten. Mark interrupted/
   unflushed writes as uncertain, accepting only documented outcomes rather
   than demanding they all roll back or all persist. Full sync establishes
   parity consistency, not arbitrary data integrity or write atomicity.
6. Run common healthy-I/O correctness workloads on md RAID5 too, using each
   implementation's actual member data offsets. Independently validate md
   layout/parity for the selected identical geometry; differential logical
   results supplement, never replace, the independent oracle. Distinguish md
   capabilities outside our scope and never declare an unsupported rublk
   operation equivalent to a successful md operation.

Correctness acceptance is zero unexpected data/parity mismatches, zero
out-of-range writes, correct durable-epoch behavior and completed required
fault/concurrency cases. A failed verification invalidates performance results
for that configuration. Save mismatch ranges, input seeds, operation histories
and raw backend evidence for reproduction.

### md RAID5 performance comparison protocol

Baseline is native md RAID5 on the same running kernel and the same three
dedicated disposable NVMe devices/partitions, run sequentially with exclusive
ownership. Never assemble md and rublk over the same active members together.
Save identities and exact recreate/cleanup procedures; do not reformat user
data. File/loop-backed fixtures are functional-test tools, not the primary
NVMe performance baseline. Real-device test setup is explicit and isolated.

Match RAID5 level, three members, 64 KiB chunks, left-symmetric layout, usable
logical capacity, sector alignment, tested LBA range, direct I/O and workload.
Record actual member offsets/metadata reservations; equalize exposed capacity
without assuming md uses the rublk offset. Initialize md properly and wait for
initial sync to complete; do not use assume-clean to skip correctness. For the
comparable recovery policy use md without bitmap/llbitmap, PPL or journal and
verify actual consistency policy; other md policies are optional labelled
experiments, never silently substituted for the baseline.

Record kernel/mdadm/fio/rublk/BPF revisions, md stripe-cache and worker settings,
CPU governor/affinity/NUMA, IRQ placement, queue counts/depths, NVMe model/
firmware/capacity, PLP/cache/flush settings, PCIe topology and temperatures.
Use identical host resource budgets, recording all md kernel workers and ublk
server/IRQ CPU cost. The default md tuning and any tuned md/rublk comparison
must be shown separately. Ublk queues do not imply one-to-one NVMe hardware
queue mapping; report observed execution/affinity instead of asserting it.

Required matrix:

| Dimension | Required cases |
| --- | --- |
| Workload | Sequential and random read/write; random read/write mixes 70/30 and 50/50 |
| Request size | 4 KiB, 64 KiB, 128 KiB, 256 KiB and 1 MiB where both configured frontends support them; record splitting/limits, no silent size changes |
| Outstanding I/O | Per-job depth 1/16/128 within common limits; record jobs and aggregate depth explicitly |
| Parallelism | One and multiple jobs/ublk queues, scaled over available CPUs; identical fio job/depth/CPU allocation for md |
| Address locality | Uniform/disjoint stripes and intentional same-stripe hotspots, with defined overlapping-write verification rules |
| Durability | Asynchronous direct-I/O baseline plus identical supported periodic fsync/FLUSH workloads; do not compare rublk rejected FUA with md successful FUA |
| Recovery | Fully synchronized steady state; full resync alone; normal workload plus full resync; rublk sync window/rate and md resync limits recorded |

Precondition the disposable SSD test area consistently, use the same data/
compressibility pattern and steady-state preparation, and report whether cache
or thermal effects remain. **Steady-state, fully synchronized** fio runs use
30 s warmup and at least 120 s measurement, five repeats per configuration,
alternating target order to reduce drift; report longer duration if needed.
Recovery runs follow the phase/window protocol below, not a mandatory 30 s
warmup that can consume the entire sync interval. Keep small deterministic
correctness fixtures separate from capacity/performance fixtures and complete
correctness before benchmarking.

### Recovery performance phases, progress and histogram windows

Use one correlated monotonic time base for workload counters, latency bins,
progress observations and recovery events per run; record timestamp origin,
poll resolution and any cross-process conversion. Capture from before sync
starts through completion/finalization and a separately labelled post-sync
phase. Do not discard early sync solely as a fixed warmup. Record workload
start/stop, recovery start, scan start/end, pause/resume, final flush start/end,
RUNNING transition and failures, plus a timestamped progress timeline. Report
process startup/recovery downtime separately from online-I/O intervals.

1. Keep steady-state and recovery datasets distinct. Recovery reports include
   active scanning/parity writing, paused recovery, finalization/flush and
   post-sync normal I/O as separate phases. A RECOVERING state alone is not
   proof that the scanner is active. Include scan-side contention and natural
   stalls in active-phase results; do not cherry-pick only high-throughput bins.
   Record each target's actual recovery lifecycle rather than assuming identical
   event semantics. Report total recovery elapsed time separately.
2. Collect timestamped 1 s latency histograms and operation/byte counts, plus
   progress samples at least once per second (record effective resolution).
   Main active-phase windows must lie wholly inside observed unpaused scan
   activity and wholly within the selected frontend-address category. If start/
   completion is known only between polls, exclude ambiguous edge bins from
   pure-phase statistics and report the omitted interval. For short runs use
   validated finer sampling and state its overhead/resolution.
3. Split a window crossing scan completion, finalization completion, pause or
   address-boundary crossing at the event, if sufficiently fine underlying
   observations exist. Otherwise label that bin MIXED/BOUNDARY and report it
   separately; never apportion aggregate counts or a histogram proportionally
   to elapsed time. Separate post-sync samples cannot inflate sync throughput
   or hide its latency. Likewise, a long I/O spanning a phase boundary is a
   boundary operation, not a pure-phase latency observation; report its count
   and latency separately. The harness must support this classification or
   conservatively exclude unresolved boundary cohorts and disclose the limit.
4. Predeclare window alignment/duration and minimum validity criteria before
   comparing implementations: default 10 s contiguous pure-active windows,
   plus all remaining usable active-phase bins and their exact durations.
   Apply the same rule to md and rublk; compare both elapsed-time windows and
   matched progress bands, with actual progress/bytes/workload duration shown.
   Do not compare one target's active phase with the other's post-sync phase.
   If full-speed sync is too short, report the actual native duration, available
   shorter windows/sample counts and percentile limitations; mark the requested
   long-window comparison unavailable rather than substituting steady state.
   Artificially throttled or paused-start experiments are separately labelled
   supplementary runs, never replacements for native full-speed recovery.
5. Foreground runs distinguish fixed LBA bands entirely behind F (already
   synchronized), entirely at/ahead of F (not yet certified), and whole-capacity
   mixed access. Log each band's offsets/length and its relation to timestamped
   F throughout the window. Predeclare bands and safety margins; when F crosses
   a band, split/reclassify or exclude that boundary window. Whole-capacity
   results retain the mixed label and report the changing behind/ahead share;
   never call the result pure RMW without describing on-demand repair traffic.
   For fine path attribution record F at permit grant and aggregate repair/
   ordinary-RMW counts with lightweight instrumentation, whose overhead is
   checked separately. Polling alone cannot prove per-request path classification.
6. For md record actual sync action/progress/position and member-to-logical
   geometry translation. Its exposed progress may not certify a per-stripe
   frontier with rublk F's semantics. Define conservative before/after bands
   only when that relation can be justified; otherwise label md spatial
   classification unknown and do not claim equivalent behind/ahead behavior.
   Whole-capacity comparisons remain valid when lifecycle windows and other
   conditions match. Record scan speed/limits and all pauses for both targets.
7. Compute each phase/window's p50/p95/p99/p99.9 from its own latency histogram
   or eligible raw observations. A combined-phase percentile requires merging
   matching histogram bucket counts; **never average window/job p99 values**.
   Preserve histogram boundaries, operation counts, boundary cohorts and logs;
   different incompatible buckets require validated rebinning or separate
   reports. Define total latency versus completion latency consistently. Report
   per-run percentiles/spread and any merged across-run histogram explicitly,
   not a fictitious overall p99 calculated from percentile averages. Compute
   IOPS/bandwidth with the corresponding eligible counts and actual durations.

Tests for the benchmark harness must include sync completing during would-be
warmup, completion in the middle of a bin, unequal target completion times,
frontier crossing a foreground band, pause/finalization, long boundary I/O,
short sync and known histogram distributions. Assert no post-sync/boundary
sample leaks into pure-sync metrics and quantiles come from the proper counts.
Preserve raw timelines and bin eligibility decisions for audit/reproduction.

Collect IOPS, bandwidth, p50/p95/p99/p99.9 latency, total CPU and cycles/I/O,
per-member bandwidth/IOPS/latency, frontend/backend write amplification and
errors. For recovery report scanned capacity, elapsed time, scan rate, normal
I/O p99 degradation, FLUSH tails, restart time and resources. Native md parity
resync is the recovery comparison, not replacement-disk rebuild. Trigger md's
actual unclean-array resync in a disposable environment; if only a manual
repair/check surrogate is possible, label it and do not equate it to crash
recovery. Separate kernel-only md recovery from userspace-server restart time.
Save fio jobfiles and JSON, host/per-member metrics and complete setup state.

Report paired absolute numbers, spread across repeats and rublk/md ratios;
do not promise a speedup or hide workloads where rublk loses. Performance runs
use release binaries with coverage disabled and no heavy tracing/fault hooks.
Correctness, coverage, diagnostic tracing and timing measurements are distinct
runs, with exact build/revision/configuration recorded for each.

### Source coverage accounting and path coverage

Use pinned `cargo-llvm-cov` and matching rustc LLVM tools for Rust source-based
coverage; verify the tool/version/options on the development architecture before
scripting. Reference: https://github.com/taiki-e/cargo-llvm-cov and
https://doc.rust-lang.org/rustc/instrument-coverage.html . Build and run both
unit/model suites and real-device integration with the RAID5 feature enabled.

1. Report line/region/function hit and total counts per file and per module
   for `src/raid5.rs` and `src/raid5/**`, including metadata, recovery, stripe
   management, flush gate, lifecycle, I/O and error cleanup. Separately report
   changed args/main integration sites and modified libublk/io-uring code;
   do not dilute the target metric with unrelated targets/dependencies.
2. Report Rust branch coverage separately only where the pinned toolchain
   supports and collects it; identify any nightly/extra requirements. If
   unavailable, mark it unavailable, not 0% or inferred from line coverage.
   Independently maintain a required state-transition/error-path scenario
   matrix: normal and cancel grants, failed recheck, sync/RMW interleavings,
   USER iterator failure, auto-reg kernel failure, metadata baseline failures,
   both slot phases, seals/create-resume, shutdown and restart fencing.
3. Integration tests spawn the instrumented server binary, not an old
   uninstrumented target/debug binary. Preserve profiling configuration through
   sudo/daemon/child processes, use a writable isolated profile directory and
   unique per-process/module filenames, then merge only matching build IDs.
   Demonstrate that queue and worker execution appears in the report. Fail
   missing-profile collection rather than reporting a misleading green run.
4. Crash/SIGKILL tests may not flush profiler data at termination. Keep their
   correctness results and missing profile limitations explicit; collect
   companion deterministic/controlled-exit fault paths for coverage, but do
   not claim that proves physical power-loss behavior. Do not change crash
   timing/durability semantics just to flush counters.
5. Standard Rust coverage does not cover a kernel-loaded C BPF program or
   md/kernel code. Report BPF verifier/load results and operation/error-branch
   test/counter matrix separately. Any optional host-compiled C coverage tests
   are supplemental and not BPF runtime instruction/branch coverage. Likewise
   kernel auto-reg failures require trace/test evidence, not invented target
   source hits. If runtime BPF coverage tooling is added, validate and report
   it as a separate metric with its own limits.
6. Exclude generated skeletons/bindings, third-party unmodified code and tests
   from primary production-code percentages, but publish the exclusion list.
   New handwritten bindings/unsafe ABI and loader paths remain auditable and
   get separate reporting if they cannot be instrumented. Do not exclude hard
   error paths merely to raise a percentage.

Deliver HTML plus LCOV or JSON, summary hit/total counts, raw profiles/build
metadata, per-suite and merged reports, uncovered source locations with reasons,
and the required scenario matrix. No arbitrary percentage is asserted before
measurement: establish/report the baseline, fix unintentional gaps, and explain
remaining unreachable/unsupported paths. Every required correctness/recovery
scenario must execute successfully regardless of headline coverage percentage;
coverage is evidence of execution, not proof of correctness or concurrency.
A skipped device suite is not successful coverage of the runtime path.

## Numbered implementation and verification plan

Execute steps in order. Each step produces a reviewable artifact and records
commands, expected versus actual results, failures and limitations. A failed
gate stops dependent steps; fix it or revise this design explicitly. Do not
label planned checks as passed. No implementation or runtime test was performed
by this document revision.

### 1. Capture baseline and safe test environment

1.1 Record kernel release and full kernel/rublk/libublk revisions, dirty trees,
Cargo lockfile, toolchain, BPF compiler/libbpf, UAPI and enabled kernel config.
Read the demo review and preserve unrelated/untracked work.
1.2 Verify the running kernel is `7.3.0-rc2-raid5-7.4+`; inspect BPF/ublk
capabilities and resource limits. Verify `/dev/ublk-control` exists.
Current machine observation (2026-10-27 document update): `uname -r` reports
`7.3.0-rc2-raid5-7.4+` and persistent GRUB_DEFAULT selects that version.
The follow-up review's 7.2 observation is stale relative to this check.
Always repeat step 1.2 at execution time; a future mismatch blocks tests and
requires explicitly selecting the development kernel before continuing.
1.3 Reserve disposable three-file functional fixtures, dedicated NVMe
comparison members and unique device IDs; record mdadm/fio/coverage-tool
prerequisites and never reuse active members between targets. Record
cleanup and tracing/sysctl restoration. Never use real user data or mounted
block devices. Derive fd/memlock needs from this implementation; do not inherit
the demo's 1024-eventfd allocation.
Acceptance: reproducible baseline and isolated fixtures; missing prerequisites
are reported as blocked/skipped, never passing device tests.

### 2. Resolve dependency packaging and ABI

2.1 Inspect user-recovery/fetch/commit flags and old-worker fencing as well as the actual libublk queue initialization and thread-local ring,
transitive io-uring versions, kernel BPF UAPI, loader and struct-ops lifecycle.
2.2 Create pinned dependency forks (or the documented vendored alternative),
add io-uring BPF typed SQE/setup support and libublk opt-in ring initialization.
Specify configuration timing, repeated initialization behavior, incompatible
configuration errors and default behavior for existing targets.
2.3 Implement the C/clang + libbpf-cargo build.rs/skeleton and libbpf-rs loader
baseline described above, proving local struct-ops support (or recording the
validated C/libbpf FFI alternative). Add C/Rust ABI layout checks and
Cargo dependency unification. Define an optional `raid5` feature for the local
extension/tooling so ordinary builds do not require the BPF toolchain.
2.4 Build ordinary targets and extension-enabled code; inspect `cargo tree`
for duplicate incompatible io-uring types. Record packaging decision and pins.
Acceptance: reproducible build with no registry-cache edits or absolute local
path dependency; existing target defaults unchanged; ABI matches pinned kernel.

### 3. Prove one-ring BPF submission (R1 gate, part A)

3.1 Build a minimal disposable harness using the new libublk API, not only a
standalone C demo. Initialize the BPF-enabled ring before queue construction.
3.2 Load/attach the BPF program, submit one SQE via `ublk_submit_sqe`, route its
CQE to the correct future, and verify expected result and output bytes.
3.3 Exercise unavailable setup flag/opcode, load/attach failure and negative
or short BPF result. Verify partial setup unwinds ring, link and allocations.
Acceptance: exact successful CQE/output plus explicit failures, no hidden XOR
or ordinary-copy fallback; save commands and trace/log evidence.

### 4. Prove KFIXED registration and failure behavior (R1 gate, part B)

4.1 Use a disposable ublk request with `BufDesc::AutoReg`, tag index and no
`UBLK_AUTO_BUF_REG_FALLBACK`. Verify ring-relative index and nonzero offsets.
4.2 Verify the pinned kernel's actual no-fallback path: inject a controlled
auto-reg failure on a disposable request and require a client-visible I/O
error, kernel trace of `AUTO_BUF_REG_FAIL`/request end, no normal delivery to
the target and no backing operation/copy fallback for that request. Do not
wait for a nonexistent target failure event or add a duplicate commit.
Verify the outstanding fetch/tag remains usable by a subsequent successfully
registered request, with no stale data/index or hang; document the actual
fetch lifecycle from kernel code and experiment. Missing registration/BPF
capabilities fail setup, while isolated runtime auto-reg failure is accepted
as a kernel-completed per-request failure, not a target-driven device stop.
A change to whole-device notification requires a kernel ABI change and is
outside the initial implementation. Record any fault-injection instrumentation
separately from product code.
4.3 Issue fixed backing reads/writes and BPF KFIXED reads. Trace
`io_uring_bpf_issue`, iterator types/offsets, fixed I/O and
`ublk_copy_user_pages`; run a plain-copy positive control with active requests.
4.4 Prove `rublk recover` lifecycle on a disposable device with user recovery
enabled at creation: terminate server with in-flight data operations, fence
old ring/backing I/O, recreate queue registrations and verify original client
outcomes without automatic request reissue or double completion. Verify a
new process cannot race old worker SQEs. Treat missing dependency/kernel
lifecycle support as a gate to fix explicitly, not an assumed wrapper feature.
Acceptance: correct bytes/offsets and demonstrably active tracing; zero request
bio-page copy events on sampled ZC paths, positive events in copy control.
Report sample scope; this does not prove all intermediates are copy-free.

### 5. Prove USER three-source RMW and buffer budget (R2/R3 gate)

5.1 Implement the bounded XOR BPF operations in the spike: two-source
full-stripe and three-source `oldP xor oldD xor newD`. Validate descriptor type,
index, offsets, length, overflow, iterator creation and complete page walks.
5.2 Document USER `import_ubuf`/page extraction/unpin semantics and stable
input mapping lifetime, without mandatory pre-registration, plus arena output
addressing. Load through the verifier while retaining two USER
iterators plus one KFIXED iterator as required by the chosen program structure.
5.3 Test known vectors for 4 KiB and 64 KiB, nonzero request offsets,
page boundaries, malformed descriptors and short/failed iteration. Require
exact requested byte count and compare output with an independent userspace
oracle used only in tests.
5.4 Prove the parity **write-back** direction: after BPF fills the real arena
slot, submit ordinary `Write` with its mapped user address via libublk to an
O_DIRECT disposable backend. Consume exact CQEs, read back independently and
compare every byte. Cover 4 KiB/64 KiB, nonzero tag/slot offsets and all tags;
verify mapping alignment, pin/import support and BPF-to-I/O visibility. In
parallel prove ordinary `Read` into accessible old-data/parity USER slots
without pre-registration. Inject import/page-extraction/pin failures and verify cleanup, no fallback, no
WriteFixed using a KFIXED tag index for an arena pointer. If this baseline is
unsupported, stop and prove/document the explicitly registered buffer
alternative (including FIXED descriptors if inputs use registered BPF lookup)
before proceeding; do not postpone the decision to steps 12–13.
5.5 Exercise all tags at maximum supported depth with disjoint three-slot
ranges and checked allocation limits; test overflow and resource exhaustion.
5.6 Prove BPF USER/USER full-parity rebuild and ordinary parity Write on the
recovery ring and on-demand requesting tag. Use independent input/output slots,
nonzero offsets and all rotations; no KFIXED request pages are copied to USER.
Acceptance: verifier/load/runtime evidence, exact parity and error results,
no overlap/out-of-range access; both backing directions succeed using the
chosen documented SQEs and real allocations, and owned storage/mappings survive
until final CQE with kernel iterator pins released on all exits. Record SQE type, index/address, length and offset per class.

### 6. Prove multi-queue ownership and teardown (R6 / final spike gate)

6.1 Start at least two dedicated queue threads concurrently; record separate
ring identities, fixed-file tables, request indices, BPF links and arenas.
6.2 Submit distinct patterns using the same tag numbers on both rings and
verify no cross-ring source/output use. Test attach failure on the second queue.
6.3 Stop queues with pending work, cancel/drain operations, consume completion
results, then detach/unregister/free on the owning thread. Test repeated startup.
6.4 Add the dedicated sync ring/worker alongside normal queues. Validate
independent BPF attachments/buffer budgets, shared stripe/gate coordination,
old-worker fencing and cancellation/drain with both classes active.
Acceptance: independent attachments, no use-after-free or resource leak,
correct rollback when any queue fails. Publish a spike report closing steps
2–6 before implementing the target data path. A failure requires design revision.

### 7. Implement and test pure geometry/layout module

7.1 Add a checked helper parameterized by `nr_disks`; enforce exactly three
backends at the target boundary. Validate capacity, segment splitting and
original-request KFIXED offsets without submitting I/O.
7.2 Unit-test three rotations, first/last sectors, chunk/stripe/capacity
boundaries, multi-chunk requests, invalid geometry and arithmetic overflow.
Use generated offsets and an independent expected-layout oracle.
7.3 Include reserved metadata/data offsets, usable capacity and final stripe;
prove normal/sync I/O never touches superblock slots and no adoption/recovery
path maps reserved bytes as user data.
Acceptance: logical data order maps to unique non-parity disks and all segments
cover the request once, within backend and KFIXED ranges.

### 8. Implement StripeWorkTable independently (R4)

8.1 Implement the fixed shard mapping and per-shard transient map plus
persistent failed state, unique tokens, listener-before-enqueue, FIFO grant,
queued guard and RAII permit. Keep locks short and perform notification/
callback/destructor work outside locks. Include failure-record allocation
failure containment before any write and cancellation handoff semantics.
8.2 Add deterministic and property/model-based tests for cancellation before
enqueue completion, while queued, after head grant and during release/shutdown.
Exercise same-stripe contention and independent stripes from different queues.
8.3 Verify persistent failed state survives transient-entry reclaim. Test
A owning a stripe, B checking healthy and queueing, A publishing failure, and
B acquiring the permit: B must recheck under the same shard lock and issue
zero backend/BPF SQEs. Repeat for a queued read and across queue threads.
8.4 Test identical stripe IDs map identically on all queues, colliding and
noncolliding stripes, wake-after-unlock with a reentrant test notifier, and
lock-order constraints. Check failed recheck and publication linearization.
Model deterministic grant/cancel-before-notify races; ensure a cancelled grant
cannot double-release or revive a reclaimed healthy entry over failed state.
8.5 Model sync versus normal read/RMW/full-stripe write on the same/different
stripes. A sync read cannot start outside its permit; on-demand repair and RMW
cannot release/reacquire between phases. Failed-state publication also blocks
queued sync owners; no sync action clears failed. Test frontier recheck races.
Acceptance: single ownership, live FIFO, no lost wake/double release, cancelled
head hands off, shard isolation, lock-free notification callbacks and persistent
failed recheck are tested. Concurrency performance remains unverified until
measured; correctness tests alone do not prove scaling.

### 9. Implement and test flush admission ordering

9.1 Implement `flush_gate.rs` with the explicit FIFO/token/listener algorithm
above. Define append/grant/release linearization points and independent cleanup
ownership. Shared writes precede stripe acquisition, exclusive FLUSH waits for
earlier shared owners; neither holds the bookkeeping mutex across an await.
9.2 Test delayed backing completion, multiple flushes, late-arriving writers,
errors and cancellation, including a granted head cancelled before notification.
Assert shared writes coexist, later writers never bypass a queued FLUSH,
multiple FLUSHes stay FIFO and all wake callbacks execute after unlock. FLUSH
must not hold a stripe permit while draining.
9.3 Unit-test/model-test continuous later write arrivals with a queued FLUSH,
hot-stripe queued admission holders, and interleaved gate/shard operations.
Device-level latency/competition measurements are deferred to steps 15/17.
9.4 Admit sync writes through the same shared gate; test FLUSH with both I/O
classes, paused/rate-limited sync and finalization seeking exclusive admission.
No rate wait/worker join under a permit; finalization holds no stripe permit.
Acceptance: no later writer passes a queued flush, no starvation/deadlock,
and all prior writes finish before three backend fsyncs start.

### 10. Integrate CLI, JSON and setup validation (R5)

10.1 Add the feature-gated module, `Raid5AddArgs`, `AddCommands`, parse/add
handlers, zero-copy whitelist and target-compatibility validation. Require
async execution if the implementation only supports the async queue lifecycle.
10.2 Implement versioned target JSON, member superblocks and recovery entry
points: fresh create/adoption, explicit existing-array assembly after reboot,
and `rublk recover` for a kernel-retained device. Enable required user recovery
at creation; reject request-reissue mode, user-copy, missing zero-copy,
unsupported geometry and non-NVMe production members. Validate member UUIDs,
order, reserved prefix, data offset, generation and capacity, not just paths.
10.3 Implement `metadata.rs`: two checksummed slots/member, conservative
all-six-slot CLEAN selection, immutable creation seals and interrupted-create
classification, trustworthy-identity checks and all-member DIRTY persistence
before any mutation. Implement the mandatory transition-entry baseline:
fence/drain old metadata/backing I/O, freeze mutations, flush all members
successfully, freshly reread/validate slots/seals, then select durable survivor.
Apply this to assembly/recover, all transition retries and create-resume;
baseline failure/uncertainty forbids any slot/seal overwrite. Implement a
reusable per-member state machine: write/verify first slot, flush, then write/verify
second slot and flush. Use it for BOTH DIRTY and CLEAN; parallelize members,
never the dependent overwrites on one member. No rollback overwrites survivors. Fresh adoption verifies data parity read-only;
unclean existing arrays enter RECOVERING rather than rejecting stale parity.
Prepare normal/sync rings and buffers; publish readiness only when all resources
and recovery concurrency guards are ready. Roll back failures without false CLEAN.
10.4 Test CLI/JSON, clean assembly, foreign/duplicate/missing members, unsupported
NVMe geometry and metadata errors. Verify fresh adoption doesn't alter data
regions; deliberate metadata writes remain within the reserved prefix.
10.5 Test initial inconsistent unformatted members reject without writes; dirty
formatted members with stale parity recover by full sync. Cover torn/checksum/
stale/mixed slots, crashes between each metadata write/flush, failed dirty
transition and failed clean transition. At every first/second slot write and
flush boundary inject crash plus torn writes, including BOTH slot writes
having been requested by a deliberately invalid schedule: assert the real
state machine never permits that schedule before the first flush. From an
established set assert at least one valid original identity slot survives per
member, generation advances safely and no false CLEAN bypasses full sync.
Test one-invalid-slot startup, short CQEs, failed/uncertain flush and member
parallelism. Add an explicit volatile-cache/durable-media test model: A10 is
durable, B11 has a successful write CQE but no successful flush; terminate
ONLY the process, retain controller cache and restart/retry. Assert baseline
flush precedes survivor selection/any overwrite and stabilizes B11; inject
power loss/torn A overwrite afterward and require a surviving identity.
Failed/uncertain baseline flush must leave A10 untouched and forbid all further
slot/seal repair. Model process exit and device power loss as different events;
reread/CRC/generation alone must not satisfy the durable-survivor precondition.
Repeat for create-resume and retries of failed DIRTY/CLEAN transitions. Separately interrupt bootstrap and each seal write/flush: normal
assembly must reject incomplete seals; explicit same-identity create-resume
revalidates parity and finishes provisioning, or refuses unrecoverable identity.
10.6 Implement `recovery.rs` coordinator/window/frontier and explicit state
transitions, progress/status/pause/rate controls, and independent cleanup owner.
Unit-test out-of-order completions, holes, failed/cancelled stripes, bounded
memory and F reset on restart. Use fake I/O here; integrate real I/O in 11–14.
Acceptance: deterministic metadata selection and state machine, no writes before
DIRTY durability, no unsafe fresh admission, no partially prepared writable
device, and user-recovery hooks proven by the spike. Missing members are refused.

### 11. Implement queue/tag lifecycle and reads

11.1 Add bounded per-queue executor draining and QueueWake bridging. Implement
the independent transaction-task registry and cancellation state machine above
before adding I/O phases. The registry, not an abortable caller future, owns
permits, write admission, registration, descriptors and pending-SQE accounting.
Dropping a result waiter signals cancellation but cannot drop the I/O owner.
Consume all owned CQEs before release/commit; commit delivered requests once,
and never commit an auto-reg failure completed solely by the kernel.
11.2 Implement validated segment reads under stripe permits, with mandatory
post-acquisition failed recheck under the owning shard mutex before any SQE,
using ReadFixed
into original-request offsets and exact CQE byte checks.
11.3 Integrate background sync transactions and USER/USER parity rebuild with
the shared permit/gate and dedicated registry. Ordinary reads in RECOVERING
use data directly after failed checks; test different-stripe concurrency.
11.4 Test all rotations and split reads, short/error completions, queue-down
and cancellation with pending reads. Seed known backing data and consistent
parity before startup, then use fresh-adoption validation or identified-array assembly as appropriate; do not
mutate active owned backends to prepare a test.
Acceptance: correct logical bytes and no early descriptor/registration release;
queue-down follows normal shutdown without a second commit.

### 12. Implement full-stripe writes

12.1 After request-wide write admission and each segment permit, recheck failed
under its shard mutex before any SQE. Build BPF inputs in logical data-index
order, calculate parity into the
new-parity slot and inspect its CQE before any data/parity write is submitted.
12.2 Submit independent `WriteFixed` KFIXED data writes and ordinary `Write`
parity writes from the proven arena user mapping (or the explicitly validated
step-5 alternative); retain ownership
until all results are consumed, even if one fails. Mark a stripe failed if any
write was submitted and its final state is partial, failed or uncertain.
12.3 Test full-stripe writes racing sync above/below F: no old-data dependency,
no early F advancement and no stale scanner parity overwriting new data.
12.4 Verify 128 KiB full stripes on rotations 0/1/2 and later stripes, 256 KiB
multi-stripe requests, exact CQEs and physical independent parity.
Acceptance: readback and physical layout/parity match; BPF failure performs no
backing writes; partial-write failure is contained and never reports success.

### 13. Implement partial-write RMW

13.1 After shared write admission and the stripe permit, recheck failed under
the shard mutex; if failed, issue no reads, BPF or writes. In RECOVERING read F
after grant; for stripe >= F, full-repair its baseline parity first, drain,
then continue RMW under the SAME permit/admission. Reuse the three slots only
after repair completes. After any required repair (or immediately for a
trusted baseline), issue ordinary `Read` for old data and parity concurrently into the
accessible, lifetime-owned USER tag slots using the path proved in step 5;
no mandatory io_uring input registration is involved.
After exact read completions run three-source BPF with new KFIXED data.
13.2 Check BPF length, then issue KFIXED `WriteFixed` data and arena-address
ordinary `Write` parity using the exact step-5 path. Serialize per-chunk
transactions within a tag and never hold two stripe permits simultaneously.
13.3 Test initially stale parity with first 4 KiB write above F, repeated
partial writes before scanner arrival, F advancing during waits and scanner
arriving before/after repair. Require physical parity after both orders and
no premature permit release. Test on-demand repair failure/cancellation.
13.4 Test 4 KiB, partial chunk, full chunk, nonzero KFIXED offsets, crossing
chunk/stripe boundaries and writes covering both data chunks. Test every
rotation and independently verify touched parity and untouched backend ranges.
Acceptance: exact logical readback/parity and no stale slot reuse; failed RMW
reads/BPF cause no request-data writes or subsequent RMW parity writes. Any
completed baseline repair is accounted for separately. Uncertain/failed writes
poison the stripe and recovery failure follows the ERROR/drain policy.

### 14. Wire FLUSH, rejection and failure containment

14.1 Connect step 9's gate to actual writes and Fsync on all three backends.
Return the first error only after all issued sync CQEs are consumed.
14.2 Reject FUA and unsupported opcodes before any backing mutation; cover
malformed/out-of-capacity requests. Define failure counters and clear errors.
14.3 Implement sync finalization and orderly stop: stop scheduling, drain both
registries, exclusive gate plus all-member flush, then RUNNING transition or
shutdown CLEAN decision. CLEAN transitions reuse the survivor-protected
slot-write/flush state machine from 10.3; no second overwrite until first-slot
flush success, and no publication of clean shutdown until every required
member stage succeeds. On error stop progression, drain pending metadata I/O
and never overwrite a survivor to roll back. Keep writable sessions DIRTY; unfinished sync/errors
never clean. Crash restarts full sync at zero; user FLUSH never marks CLEAN.
14.4 Recheck failed under the shard mutex after every read/write permit grant;
pre-enqueue checks alone are insufficient. Publish failed before permit handoff
and reject subsequent reads/writes to failed stripes without submitting I/O. Document that this
failed set is in-memory; DIRTY restart performs full sync but cannot repair
underlying unreadable/corrupt data or promise degraded recovery.
Acceptance: ordering trace agrees with gate tests, no false flush success,
no writes for rejected operations, and failed-stripe behavior is deterministic.

### 15. Run fault, concurrency and shutdown matrix

15.1 Inject each backend read/write/fsync failure, negative/short CQEs, BPF
iterator/result failures and auto-reg failures at each relevant phase.
15.2 Run overlapping readers/writers, same/disjoint stripes, queue depths
1/16/128 within configured limits, one and multiple queues. Check completed
operations against permitted serialization and independent physical parity.
15.3 Cancel before/after stripe grant and with pending reads, BPF and writes.
Hold a submitted write CQE artificially pending, cancel its caller, queue a
same-stripe RMW successor and a FLUSH, and assert neither can pass the owner's
permit/admission until every original CQE is consumed and failed-stripe state
(if required) is visible. Repeat with partial submission, cancel-CQE success
before original CQE, queue shutdown and cancelled FLUSH with pending fsync.
Test that the independent task survives dropping the result waiter; permit
Drop is never the async drain mechanism. Stop under load and repeat creation/
deletion; check exactly-once commit, no early tag reuse, no leaked resources.
15.4 Force an already queued reader/writer to receive a permit immediately
after its predecessor marks failed. Require `-EIO` with zero new backing/BPF
SQEs; repeat after transient waiter-entry reclaim and on another queue. Stress
shard collisions, lock-outside-notify, continuous writes with queued FLUSH and
hotspot backlog, checking lock order and no later-admission bypass.
15.5 Repeat fresh-adoption parity corruption/first-4-KiB-write rejection and
kernel-only auto-reg failure case, including subsequent successful requests.
Distinguish a kernel-ended request from a delivered target transaction.
15.6 Run concurrent sync with same/different-stripe reads, full writes, 4 KiB
RMW, split requests and FLUSH across queues. Delay scanner reads/writes/CQEs,
exercise both ownership orders and frontier holes, and independently XOR each
stripe after stable drain. Verify on-demand repair before old-parity use.
15.7 Kill server during each recovery/metadata/data phase, then test retained
ublk recovery and reboot-style assembly. Repeat interruption during sync;
require F=0 and full scan on restart, no old-worker writes after new ownership,
no double completion, no false CLEAN and eventual correct parity. Use real
VM power-cut tests where available, not only orderly SIGTERM; distinguish
process-kill tests from durable-media/power-loss evidence. For established
arrays, enumerate both DIRTY and CLEAN transitions, every member, each first/
second slot write and flush boundary, with truncation/torn-sector injection
and all inter-member stage interleavings represented by the model. Assert
per-member survivor identity, conservative dirty assembly and zero false CLEAN.
A second overwrite must be absent until the first flush is confirmed; failures
stop advancement and pending operations drain before retry. Add the distinct
sequence `slot-write CQE -> no successful flush -> process exit -> restart
with device cache preserved -> baseline flush -> retry -> power loss/torn
write`. Test both DIRTY/CLEAN entry and create-resume, including failed or
uncertain baseline flush, on each member. Require a durable survivor and no
slot/seal overwrite before successful baseline and fresh validation. A harness
must explicitly preserve volatile cache across process restart and discard it
on simulated power loss; orderly shutdown or VM reboot alone does not prove
this case. Record model/fault-device tests separately from physical hardware
power-loss tests and establish prerequisite old-I/O fencing. Repeat separately
for initial identity bootstrap/seal completion and explicit create-resume;
never count rejected half-creation as failed established-array recovery.
15.8 Test sync/repair read/BPF/write/flush failure, cancellation, pause/resume,
metadata transition errors, resource exhaustion, and missing/foreign members.
Verify ERROR/dirty, safe admission stop and drained cleanup with locks retained
until original CQEs finish; no skipped hole or successful recovery on failure.
15.9 Implement the independent expected-content/layout/parity verifier and
required correctness matrix above. Cover full logical fixture readback,
physical parity, metadata canaries, untouched ranges, deterministic payloads,
concurrent histories and durable crash epochs. Run common healthy workloads
on md as a differential control with independently checked actual offsets.
Publish correctness results and retain mismatch artifacts; any unexpected
mismatch blocks acceptance and invalidates associated benchmark results.
Acceptance: no lost wake, corruption, deadlock or success after short CQE;
record seeds, repetitions and timeouts so failures can be reproduced.

### 16. Prove zero-copy execution on the integrated target

16.1 Repeat active tracing and plain-copy positive control for full-stripe,
RMW and reads across rotations, nonzero offsets and multiple queues.
16.2 Include recovery/on-demand-repair overlap in tracing: distinguish USER
reads of existing backend data from forbidden copying of KFIXED request pages,
and identify background versus normal-ring BPF/parity traffic.
16.3 Correlate request IDs/tags, BPF bytes, iterator types and fixed backing
I/O; report target-visible errors separately from kernel-observed auto-reg
failures, and verify no fallback using tracing/positive controls. Do not
assert an unavailable per-request target notification or counter.
Acceptance: sampled request data uses KFIXED without bio-page userspace copies;
separately report old-data/parity traffic and arena storage. Explain trace
coverage and omissions rather than claiming universal copy freedom.

### 17. Benchmark and regress existing targets

17.1 Measure 4 KiB RMW and 128 KiB full-stripe throughput and p50/p99 latency
at depths 1/16/128, same/disjoint stripes and one/multiple queues. Record CPU,
BPF time, lock/flush wait, backing type, queue geometry and exact revisions.
Use explicit duration/warmup, fixture size and raw output in the report.
17.2 Measure lock scalability separately: shard counts 1/16/64/256, disjoint
stripes versus read-only/mixed same-stripe hotspots, increasing queue/CPU count,
with mutex wait/hold histograms and per-queue CQE progress. Measure global
admission cost without FLUSH (including an isolated gate microbenchmark), and
FLUSH p50/p99/max with hot-stripe backlog and continuous multi-queue writers.
Use ordering traces to verify absence of later-writer bypass; no-flush/flush
runs quantify gate cost and backlog, not just total throughput. Report shard
count/hash/configuration and fairness progress under eventual completion.
Keep exclusive per-stripe permits initially; reader sharing is future work
only after these results, and no performance/scalability acceptance is claimed
by this design review.
17.3 Measure full-sync elapsed time on actual target NVMe capacities/topology,
steady-state bandwidth, CPU/XOR, metadata startup cost and restart time; report
user-data reads versus parity-write traffic separately. Compare sync paused/
active, window/rate settings, frontend p50/p99, same-stripe RMW above/below F,
FLUSH tails and normal workload interference. Do not infer from vendor peak
bandwidth. Record PLP/cache/flush settings and resource budgets.
17.4 Run supported existing-target regressions with the pinned dependencies;
build default/no-default and RAID5 feature variants, formatting and relevant
unit/integration suites. Confirm device tests executed rather than silently
skipping due to missing prerequisites.
17.5 Build the reproducible native-md comparison harness following the protocol
above: same disposable NVMe set, equal usable capacity/layout/chunk, no bitmap/
PPL/journal, completed initialization and recorded md tuning/resources. Ensure
only one target owns the members and verify correctness after recreation.
17.6 Run the paired fio matrix using separate steady-state and recovery
sampling rules above. Capture synchronized event/progress/histogram timelines;
report pure-active, boundary/mixed, paused, finalization and post-sync datasets
separately. Compare declared elapsed-time/progress windows, not unequal mixtures
of sync and post-sync. Exercise foreground already-synced, not-yet-synced and
whole-capacity mixed bands, recording F/path counts and md progress semantics.
If native sync is too short, report duration/sample limits; throttled runs
remain separate. Compute window/phase p99 from matching histogram counts,
never averaged percentiles. Validate the harness with short/mid-bin completion,
band crossing and known histogram tests. Preserve repeat/order/direct-I/O/
durability controls, collect CPU/member metrics/write amplification/sync time,
and publish raw fio jobs/JSON, bin histograms, progress/events, eligibility
rules and paired ratios/spread. Mark unavailable comparisons explicitly;
a file-backed proxy does not complete the real-NVMe comparison.
17.7 Add coverage build/run/report automation for unit/model/device suites,
instrumented child/daemon collection and separate changed-dependency reports.
Verify profiles from real RAID5 workers are collected; emit HTML and LCOV/JSON,
counts, exclusions and uncovered lines. Report branch tooling limits and BPF
path coverage separately; do not run performance benchmarks instrumented.
17.8 Review uncovered paths against the mandatory correctness/recovery scenario
matrix, add meaningful missing tests, rerun affected suites and regenerate the
report. Track crash-profile limitations honestly and require all mandatory
scenarios independently of aggregate coverage. Archive coverage build identity,
tool versions, raw profiles and skipped-check reasons.
Acceptance: reproducible measurements, explicit skipped checks, no unexplained
existing-target regression. Performance results inform later optimization;
do not weaken correctness/CQE boundaries to improve a benchmark.

### 18. Prepare final documentation and manual review

18.1 Document installation/build, required kernel ABI, CLI examples, inspection,
resource limits, safe deletion, recovery/assembly/status/pause procedures and
troubleshooting. Document NVMe-only scope, all-members-present full sync, no
bitmap/checkpoint, on-demand repair overhead and lack of degraded/rebuild or
combined-failure write-hole protection. Distinguish sync from ublk restart.
18.2 Assemble a fixed-base manual-review handoff covering dependency pins,
source changes, all gate results, the independent data/parity correctness
matrix, native-md paired performance results, source coverage/path-coverage
reports with uncovered-gap analysis, raw test/trace/benchmark artifacts and
responses to R1–R6. Do not automatically invoke review scripts.
18.3 Resolve review findings, repeat affected checks and record remaining
limits. Only claim prototype completion when all required gates pass; skipped
or blocked required checks keep the prototype incomplete.
Acceptance: implementation, independent manual review and recorded verification
support the stated prototype contract, without production-RAID claims.

## Review disposition and completion boundary

| Finding | Design response | Runtime evidence still required |
| --- | --- | --- |
| R1 | Explicit two-dependency fork/vendor and ABI plan | Steps 2–6 |
| R2 | Checked three-slot per-tag budget and total queue budget | Steps 5, 13, 15 |
| R3 | USER page extraction/lifetime (no required pre-registration), mixed iterators and arena parity write-back | Steps 4–5, 12–13 |
| R4 | Standalone token/permit module and state-machine tests | Steps 8, 15 |
| R5 | CLI/add/zero-copy/JSON plus supported recovery/assembly integration sites | Steps 4, 10, 15 |
| R6 | Corrected thread-local fact; dedicated queue thread/ring policy | Steps 2, 6 |
| R7 (follow-up) | Explicit three-memory-class SQEs and arena parity write-back/pinability gate | Steps 5, 12–13 |

Do not copy SMR's fallback-capable `smr_io_task`; reuse only verified fixed-I/O
helpers with exact-length checks. The spike must prove kernel-only auto-reg
failure semantics, no fallback, and continued fetch/tag operation; it must not
assume a libublk per-failure notification. Model/generalized layout support does
not expand the first-release CLI beyond three disks.

Acceptance for this prototype requires correct data/parity on every rotation,
proven KFIXED+BPF execution without bio-page userspace copying, exact CQE
validation, no stripe ownership leaks under faults, and an honest statement
of the write-hole/degraded-operation limits. This revision specifies that
work; it does not claim any of those implementation gates have passed.

### Follow-up review responses (2026-10-27, developer response only)

| Finding | Chosen design change | Required implementation evidence |
| --- | --- | --- |
| P1-1 initial parity | Fresh adoption verifies/rejects inconsistent input; identified dirty arrays rebuild parity with sync/normal-I/O interlock | Steps 10–15; unformatted corrupt set rejects, dirty-array RMW pre-repairs before use |
| P1-2 cancellation ordering | Independent queue-owned transaction task drains original CQEs, publishes failure if needed, then releases stripe permit/write admission | Steps 8–9, 11, 15.3; delayed-CQE successor/FLUSH test |
| P2-3 USER semantics | Plain USER inputs require accessible stable mappings, not fixed registration; optional registered BPF inputs must use FIXED | Step 5; import/extract/unpin errors and mixed-iterator lifetime tests |
| P2-4 auto-reg failures | Accept kernel-only per-request I/O failure with no fallback; no invented target event or device stop | Step 4.2, 15.5, 16; external trace/client error and subsequent request progress |

These are specification corrections based on the pinned kernel's
`io_bpf_import_buffer`, `bpf_iter_uring_buf_next`, `iter_unmap_page`,
`ublk_auto_buf_register` and `ublk_auto_buf_dispatch`. They are not device-test
results. Historical review claims about USER registration are answered here;
the developer does not modify the reviewer's document.

### Concurrency review response (2026-10-27)

1. Replace the global stripe management mutex with fixed hashed shards; each
   shard synchronizes transient FIFO state and persistent failure records.
2. Mandatory failed recheck under the shard lock after every permit grant,
   before any SQE; failure publication precedes handoff. Add the queued B after
   failed A test for both read and write paths.
3. Retain exclusive per-stripe read/write ownership in the first release and
   document read-hotspot serialization. Fair reader/writer permits remain a
   measured follow-up; never weaken whole-stripe write exclusion.
4. Specify a target-local FIFO asynchronous flush gate and lock order, with
   all notifications outside bookkeeping locks. Measure gate/shard contention
   and FLUSH tail latency; do not reorder admission behind stripe acquisition.
5. Prior design-review acceptance addressed specification findings only;
   concurrency correctness, fairness and scaling still require steps 8–9,
   11–17 evidence. No code or device tests are claimed by this revision.

### Scope extension: NVMe recovery with concurrent full sync

This user-requested extension supersedes the previous no-recovery boundary
and mandatory read-only scan on every restart. The prior design review covered
the earlier scope; this added protocol needs its own review and implementation
evidence. No bitmap/PPL/journal is added. Normal writes do not update persistent
region metadata; startup/shutdown use conservative member dirty/clean records.
All-members-present sync, online foreground concurrency, on-demand RMW baseline
repair and ublk restart/assembly are required, not optional follow-up work.

Implementation remains an 18-step plan: dependency/restart and USER/USER spike
in 2–6, layout/ownership/gate in 7–9, metadata/coordinator in 10, concurrent real
I/O in 11–14, crash/race/persistence tests in 15–16 and NVMe benchmarks in 17.
Any older review-response wording is historical if it conflicts with this
explicit scope extension. Reviewer-owned documents remain unchanged.

### Recovery metadata review response: durable survivor ordering

The review's P1 is accepted. Both DIRTY and CLEAN updates now require a
per-member durable survivor and ordered `write(T), flush, write(S), flush`;
all-member DIRTY completion precedes any data/sync mutation. Members can run
concurrently, but a second-slot overwrite can never race its first flush.
First creation is separately classified using immutable completion seals and
explicit identity-preserving create-resume; incomplete provisioning is never
silently assembled. Steps 10.3/10.5, 14.3 and 15.7 implement/test these rules.
CRC checks remain damage detection, not a claimed 4 KiB atomicity guarantee.
This is a design correction; no crash/persistence test is claimed as executed.

### Recovery metadata review response: restart/retry durability baseline

The follow-up P1 is accepted: reread plus CRC and generation ordering does
not prove NVMe persistence after process-only restart. Every transition entry
now fences old I/O, freezes mutation, successfully flushes all members, then
freshly reads/validates records before survivor selection. Failed/uncertain
baseline flush prohibits overwrite. The same rule covers create-resume and
failed-transition retries. Steps 10.3, 10.5 and 15.7 distinguish process death
with retained controller cache from power loss and test the A10/B11 schedule.
No runtime or physical power-loss validation is claimed by this doc revision.

### Verification scope extension: md comparison, correctness and coverage

Required delivery now includes independent end-to-end data AND physical-parity
verification (15.9), real-NVMe native md RAID5 paired benchmarks including full
sync interference (17.5–17.6), and reproducible Rust coverage plus separately
labelled BPF/error-path evidence (17.7–17.8). Step 1 records prerequisites and
18.2 includes these reports for manual review. The 18-step top-level order is
unchanged. These are planned acceptance requirements, not measured results;
no percentage, speedup or device-test pass is claimed by this revision.

### Performance review response: recovery sampling windows (P2)

Accepted: fixed 30 s/120 s timing applies only to synchronized steady state.
Recovery now has event/progress-qualified windows, spatial relation to F,
separate boundary/finalization/post-sync reporting, short-native-sync limits
and explicitly supplemental throttling experiments. Quantiles use eligible
histogram counts, never averages of p99. Step 17.6 includes harness validation
and raw timeline/bin artifacts; no benchmark or coverage result is claimed.
