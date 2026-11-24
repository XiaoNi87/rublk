# Experimental BPF-assisted RAID5 target

This fork publishes the RAID5 prototype for discussion and review. It is based on upstream rublk commit `c1178a43622b24e0fa491bd0f1b4a4c4bafa6614`, not rebased onto the latest upstream. It is not a production release or an upstream-merged feature.

The implementation uses three members, 64 KiB chunks (128 KiB logical stripes), ublk KFIXED request buffers, BPF-assisted parity, sharded stripe coordination, FLUSH admission, persistent array metadata and full-array parity synchronization after unclean shutdown. It does not claim general degraded-array recovery, replacement-disk rebuild or write-hole protection. See RAID5_DESIGN.md for the detailed intended boundaries.

## Dependencies and build

A normal upstream kernel is insufficient. The tested extension ABI comes from custom kernel commit `42abd4d4c48750e82cb24f52d23ab5c07b2d81c9`, tested as `7.3.0-rc2-raid5-7.4-kvm`. It supplies the required io_uring BPF/KFIXED/arena interfaces. The corresponding kernel patch series is not included in this repository; reproducing device tests requires obtaining that exact compatible kernel separately. Pinned ABI headers are under src/raid5/bpf/uapi. Do not treat those headers as a kernel implementation.

Cargo.toml patches libublk and io-uring to the checked-in vendor directories. These changes are prototype dependencies, not claims of upstream library support. Build on Linux with Rust/Cargo, a C compiler, clang with BPF support, libelf and zlib development dependencies, and the libbpf build prerequisites:

```sh
cargo build --locked --release --no-default-features --features raid5
cargo test --locked --no-default-features --features raid5 --lib
./target/release/rublk add raid5 --help
```

The validation binary used default(vram)+raid5 features; disabling default features above avoids the optional OpenCL target but is not the exact measured binary configuration. Building the measured feature set requires the OpenCL dependencies as well.

## Validation scope

The tested prototype completed functional checks in a dedicated Linux KVM guest. The performance collection contains 550 steady measurements (55 configurations, two targets, five repeats) and 50 recovery collections against Linux md RAID5. A checkpointed continuation reused 314 verified completed measurements and ran only the remaining 236; this is explicitly a multi-segment experiment. See docs/raid5/PERFORMANCE_TABLE.md for results and scope.

These are QEMU virtual NVMe results backed by a shared host ZFS storage pool, not physical NVMe performance. Some wide-range writes exhibited multi-second tail latency on both implementations; those observations remain unexplained at the underlying backend level. Recovery collections do not all have spatially qualified performance windows. No new coverage percentage is claimed for this publication. Physical NVMe acceptance, real hardware power-loss persistence and independent final evidence review remain outstanding.

## Test harness notes

scripts/raid5 contains development and fault-injection tools, including destructive device preparation. Several scripts retain original absolute paths and serial/device assumptions. They are supporting development material, not portable unattended acceptance commands. Inspect and adapt the environment and disposable device selection before use. No credentials, guest keys, VM images or raw local validation archives are included.

The original design is retained as historical reference. Publication documentation changes do not constitute another device-test run or a new validation of the executable. The upstream license and history are retained.
