# Vendored Rust dependencies

The root Cargo.toml uses `[patch.crates-io]` to select these local libraries.
They are required by the experimental RAID5 io_uring BPF / ublk registration
interfaces, not a replacement kernel. They include local changes and are not
pristine upstream snapshots. The root Cargo.lock is the application lockfile;
there are no nested lockfiles.

## Source and licensing

| Directory | Package | Upstream repository | Packaged source commit |
|---|---|---|---|
| io-uring | io-uring 0.7.12 | https://github.com/tokio-rs/io-uring | 44c9320214fdcb27ac8cc44189f7ef0e89bdde87 |
| libublk | libublk 0.4.7 | https://github.com/ublk-org/libublk-rs | 4687c539c1d43f535793c5df601533e37721094e |

Each `.cargo_vcs_info.json` records the source package's original commit, not
this fork's locally modified revision. Consult the Git history for the local
changes. Keep LICENSE-MIT and LICENSE-APACHE and the upstream copyright notices.
The package README files are retained as upstream API/background documentation;
commands referring to upstream examples/CI apply to the full upstream projects,
not this deliberately reduced vendor tree.

## io-uring file inventory

| File | Role |
|---|---|
| Cargo.toml | Actual library manifest, features, dependencies and build script selection |
| .cargo_vcs_info.json | Original packaged source revision for provenance; not compiled |
| LICENSE-MIT, LICENSE-APACHE | Dependency's dual-license texts |
| README.md | Upstream library/API usage reference |
| build.rs | Rust binding selection/generation configuration; optional bindgen/overwrite support |
| src/lib.rs | Public crate entry point and IoUring handle |
| src/bpf.rs | Local experimental BPF descriptors, flags and ring setup definitions |
| src/opcode.rs | Typed SQE builders, including the local BPF operation |
| src/squeue.rs | Submission queue entries, access and flags |
| src/cqueue.rs | Completion queue entries and consumption |
| src/submit.rs | Submission, wait and syscall handling |
| src/register.rs | io_uring resource registration operations |
| src/types.rs | Public file/buffer/probe and operation argument types |
| src/util.rs | Internal queue/buffer helper utilities |
| src/sys/mod.rs | Select checked-in, generated or explicitly supplied low-level Rust bindings |
| src/sys/sys.rs | Dispatch to the matching target architecture |
| src/sys/sys_x86_64.rs | x86-64 ABI bindings |
| src/sys/sys_aarch64.rs | AArch64 ABI bindings |
| src/sys/sys_riscv64.rs | RISC-V 64 ABI bindings |
| src/sys/sys_powerpc64.rs | PowerPC 64 ABI bindings |
| src/sys/sys_loongarch64.rs | LoongArch 64 ABI bindings |

Architecture-specific Rust bindings are compiled code selected by the crate.
They are not copied kernel C headers and must not be deleted just because the
current test VM is x86-64. The RAID5 native-build validation does not certify
all these architecture variants.

## libublk file inventory

| File | Role |
|---|---|
| Cargo.toml | Actual manifest, library/features, permission utility and regression test target |
| .cargo_vcs_info.json | Original packaged source revision for provenance; not compiled |
| LICENSE-MIT, LICENSE-APACHE | Dependency's dual-license texts |
| README.md | Upstream control/queue/unprivileged-device usage reference |
| src/lib.rs | Public module exports, common flags and errors |
| src/ctrl.rs | Synchronous device control, parameters and lifecycle |
| src/ctrl_async.rs | Asynchronous control operations |
| src/io.rs | Queue/request handling, fixed buffers and local BPF ring integration |
| src/io_batch.rs | Batch transport submodule included from io.rs using a path attribute |
| src/uring_async.rs | Async SQE submission, CQE dispatch and executor integration |
| src/helpers.rs | Aligned buffers and supporting memory/resource helpers |
| src/sys.rs | Low-level UAPI type/constant re-exports |
| src/bindings.rs | Buffer-registration address encoding helper re-exports |
| src/test_helpers.rs | Helpers used by inline library tests, compiled only with cfg(test) |
| tests/basic.rs | Upstream library regression test target; retained for dependency maintenance |
| utils/ublk_user_id_rs.rs | Declared ublk_user_id binary for reading unprivileged-device owner IDs |
| utils/ublk_chown.sh | Documented udev permission helper calling that binary; package metadata references it |

The inline tests and basic regression target are useful even though rublk's
normal dependency build does not execute them. Some require root/kernel device
support and may create devices: inspect them before running. Their development
dependencies remain intentionally present. Permission tools are not required
by every RAID5 invocation, but remain part of libublk's supported setup path.

## Removed packaging/development material

- Upstream nested GitHub CI/release workflows, mkosi/CI configurations, editor
  formatting/ignore files and AI-agent instructions.
- The dependency's standalone Cargo.lock; the application uses its root lock.
- Cargo.toml.orig, which was a pre-normalization packaging copy, not an active
  manifest and not a record of this fork's current modifications.
- Six standalone upstream examples (io-uring readme/tcp_echo and libublk
  batch/loop/null/ramdisk) and their `[[example]]` declarations. The corresponding
  unused io-uring example-only dev-dependencies were also removed.

No library/runtime source, architecture bindings, license or source-revision
metadata was removed in this cleanup. rublk's own RAID5 probes, tests and
examples outside vendor are unaffected. Kernel UAPI/BTF prerequisites are
specified in ../docs/raid5/KERNEL_ENVIRONMENT.md and supplied externally.
