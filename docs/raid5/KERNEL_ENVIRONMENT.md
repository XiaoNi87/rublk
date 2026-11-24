# RAID5 kernel, build and runtime environment

This is an experimental Linux-only target. Kernel UAPI and BTF are external
build inputs, not rublk source files. The RAID5 implementation is enabled only
by the `raid5` Cargo feature. Normal builds without that feature do not require
a patched kernel, BTF, bpftool or a BPF-capable clang.

## 1. Obtain the matching patched kernel

The tested development kernel tree was at commit
`42abd4d4c48750e82cb24f52d23ab5c07b2d81c9`. Its KVM build reported
`7.3.0-rc2-raid5-7.4-kvm`; another build reported
`7.3.0-rc2-raid5-7.4+`. These are custom local version strings, not upstream
Linux releases which inherently provide the required interfaces.

**Reproduction dependency:** the corresponding kernel patch series/tree is not
published in this fork. Obtain the exact experimental tree from the prototype
kernel provider before proceeding. The hash above is an identity of that tree,
not a commit which can be assumed to exist in torvalds/linux. Do not substitute
an arbitrary kernel with the same version string. A public fetch URL and
base-plus-patch-series are still required to make this fork independently
reproducible; this document does not invent such a URL or patch identity.

Required kernel-side extensions are:

- `IORING_SETUP_BPF_EXT` and `IORING_OP_BPF`, io_uring BPF struct-ops attachment,
  `uring_bpf_data` and explicit result completion.
- The io_uring buffer iterator/dynptr kfuncs, including
  `bpf_iter_uring_buf_new/next/destroy` and `bpf_uring_buf_dynptr`.
- USER and KFIXED `io_bpf_buf_desc` sources, BPF arena support and the sleepable
  parity program's kfunc/helper interfaces.
- The ublk auto-buffer-registration/KFIXED and registered-buffer lifecycle
  interfaces used by the pinned libublk and io-uring fork changes.

The compile-time ABI check currently requires opcode 65, BPF setup bit 21,
operation shift 24 and the specified descriptor/SQE layout. It is a guard for
this prototype, not a new portable ABI. If those checks fail, stop: either use
the matching tree or review and port the kernel and user-space changes together.
Do not edit numeric constants just to get past the check.

## 2. Build the kernel with BTF

Example for a native Debian/Ubuntu development machine (adapt packages for the
distribution). Keep the source and build directories outside this repository:

```sh
sudo apt-get install build-essential bc bison flex libssl-dev libelf-dev \
    dwarves clang-19 llvm-19 bpftool zlib1g-dev pkg-config rsync cpio
export RAID5_KERNEL_SRC=/absolute/path/to/patched-linux
export RAID5_KERNEL_BUILD=/absolute/path/to/patched-linux-build
export RAID5_KERNEL_HEADERS=/absolute/path/to/patched-linux-headers

git -C "$RAID5_KERNEL_SRC" rev-parse HEAD
# Require the tested hash, or document an explicitly reviewed port.
make -C "$RAID5_KERNEL_SRC" O="$RAID5_KERNEL_BUILD" defconfig
"$RAID5_KERNEL_SRC/scripts/config" --file "$RAID5_KERNEL_BUILD/.config" \
    -e BPF -e BPF_SYSCALL -e BPF_JIT -e NET -e IO_URING \
    -e IO_URING_BPF -e BLK_DEV_UBLK -e DEBUG_INFO \
    -d DEBUG_INFO_NONE -e DEBUG_INFO_DWARF5 -e DEBUG_INFO_BTF
make -C "$RAID5_KERNEL_SRC" O="$RAID5_KERNEL_BUILD" olddefconfig
# Check the resulting .config: unmet dependencies can turn options off.
make -C "$RAID5_KERNEL_SRC" O="$RAID5_KERNEL_BUILD" -j"$(nproc)"
```

A working machine's existing config may be preferable to `defconfig`; preserve
its boot/storage/virtualization drivers and use `olddefconfig`. In the tested
KVM config BPF, BPF_SYSCALL, BPF_JIT, NET, IO_URING, IO_URING_BPF, BLK_DEV_UBLK,
DEBUG_INFO_DWARF5 and DEBUG_INFO_BTF were enabled. BTF generation needs a
compatible pahole/dwarves toolchain. Test fixture suites additionally use FUSE;
it is not required merely to compile the RAID5 target.

Install using the distribution's kernel installation/bootloader workflow.
On machines supporting the kernel make install targets, the usual commands are:

```sh
sudo make -C "$RAID5_KERNEL_SRC" O="$RAID5_KERNEL_BUILD" modules_install
sudo make -C "$RAID5_KERNEL_SRC" O="$RAID5_KERNEL_BUILD" install
# Update initramfs/bootloader as required by the distribution; choose this kernel.
# Reboot into it before device tests.
```

Do not replace the running kernel merely to compile rublk. Compilation can use
the built kernel's ELF BTF; running the target requires booting the compatible
kernel. Retain a known-working boot entry and the matching build artifacts.

## 3. Export UAPI; supply BTF to the rublk build

Use the kernel's export process, not direct includes from `include/linux` and
not manually copied individual headers:

```sh
make -C "$RAID5_KERNEL_SRC" O="$RAID5_KERNEL_BUILD" \
    INSTALL_HDR_PATH="$RAID5_KERNEL_HEADERS" headers_install
export RUBLK_RAID5_UAPI_DIR="$RAID5_KERNEL_HEADERS/include"
export RUBLK_RAID5_BTF="$RAID5_KERNEL_BUILD/vmlinux"
mkdir -p "$RAID5_KERNEL_BUILD/bpftool"
make -C "$RAID5_KERNEL_SRC/tools/bpf/bpftool" \
    OUTPUT="$RAID5_KERNEL_BUILD/bpftool/" -j"$(nproc)"
export RUBLK_RAID5_BPFTOOL="$RAID5_KERNEL_BUILD/bpftool/bpftool"
export RUBLK_RAID5_CLANG=clang-19
export RUBLK_RAID5_CC=cc
```

`RUBLK_RAID5_UAPI_DIR/linux/io_uring.h` must contain the patched UAPI. The build
compiles `scripts/raid5/check_abi.c` against this export. It does not link or
execute the C probe. Standard libc/system headers still come from the native
compiler's normal include paths. Native builds are supported; cross builds
are rejected until target C ABI/toolchain handling is implemented.

`RUBLK_RAID5_BTF` accepts either a kernel ELF file with a `.BTF` section
(`vmlinux`) or raw BTF. If already booted into the matching kernel, use:

```sh
export RUBLK_RAID5_BTF=/sys/kernel/btf/vmlinux
```

Use the bpftool built from the matching tree: its C BTF dump must emit kfunc
prototypes, including `bpf_arena_alloc_pages`. The tested distribution tool
(bpftool 7.1.0 / libbpf 1.1) omitted those declarations and is too old for this
build path. The build checks for this prerequisite before invoking clang.

The rublk build runs the equivalent of:

```sh
"$RUBLK_RAID5_BPFTOOL" btf dump file "$RUBLK_RAID5_BTF" format c > /tmp/raid5-vmlinux.h
```

The actual generated header is written only to Cargo `OUT_DIR/vmlinux.h` and
included by clang from that directory. BPF objects and Rust skeletons are also
generated under `OUT_DIR`. No kernel header needs to be copied into `src/`.
Do not use the demonstration `/tmp` output as a new checked-in header.

The build tracks the input paths and tool-selection environment variables.
After switching kernels or replacing exported headers/BTF in place, run
`cargo clean -p rublk` before rebuilding to avoid reusing stale generated files,
especially with sysfs BTF. Keep UAPI and BTF from the same patched kernel tree.
A static layout check does not prove that the running kernel matches these
inputs; the operator must verify that and run the capability probe below.

## 4. Build rublk and check the runtime

This fork's Cargo.lock pins dependencies and Cargo.toml selects pinned Git forks of the
Rust bindings/extensions (see DEPENDENCIES.md). Use a native Linux Rust toolchain; the validation
build used Rust 1.96.0, libbpf-cargo/libbpf-rs 0.27.2 and clang 19.

```sh
cargo build --locked --release --no-default-features --features raid5
cargo test --locked --no-default-features --features raid5 --lib
cargo build --locked --release --no-default-features --features raid5 --examples
uname -r
# If ublk is modular:
sudo modprobe ublk_drv
sudo ./target/release/examples/raid5_bpf_probe
./target/release/rublk add raid5 --help
```

Inspect the probe result; do not continue after unsupported ABI, verifier or
kfunc errors. The published measurements used `--features raid5` with default
features enabled (default includes the OpenCL vram target); that exact build
also needs OpenCL development libraries. Disabling defaults above avoids that
unrelated dependency and is not the exact measured binary configuration.

Use only disposable, exclusively owned members for device tests. Creation,
initialization and fault-injection scripts can overwrite them. Consult the
CLI and design boundaries first; do not blindly execute environment-specific
scripts, which still contain the original test machine's paths and serials.
No fallback to a normal upstream kernel or copied userspace request buffer is
promised. No root requirement applies merely to compiling the project; the
runtime probes/device setup need the appropriate privileges.

## 5. vmlinux, vmlinux.h, BTF and vmcore are different artifacts

| Artifact | Produced by | Use |
|---|---|---|
| `vmlinux` | Kernel link/build | Kernel ELF, symbols and (with the config above) BTF; valid build input for this target |
| `/sys/kernel/btf/vmlinux` | Running kernel | Raw BTF for that running kernel; valid alternative build input |
| `vmlinux.h` | bpftool BTF dump, invoked by build.rs | Generated C type declarations for BPF compilation; lives in Cargo OUT_DIR |
| `vmcore` | kdump after a kernel crash | Crash memory dump, not a normal compile/runtime dependency |
| `System.map` / debug symbols | Kernel build/package | Optional debugging, not a substitute for BTF |

For crash analysis keep the exact crashed kernel's unstripped/debug `vmlinux`,
config and modules together with `vmcore`, then use the distribution's crash
or drgn workflow. A compressed boot image or vmcore cannot simply replace
`RUBLK_RAID5_BTF`. The compiler does not consume vmcore.

The new build-input workflow must be validated on Linux after changes. Earlier
performance numbers describe the originally frozen binary; rebuilding with
external headers does not transfer its binary hash or constitute a new
performance/hardware-persistence acceptance run.
