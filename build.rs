fn main() {
    #[cfg(feature = "raid5")]
    build_raid5();
}

#[cfg(feature = "raid5")]
fn build_raid5() {
    use std::{env, fs, path::PathBuf, process::Command};

    fn required_path(name: &str) -> PathBuf {
        PathBuf::from(std::env::var_os(name).unwrap_or_else(|| {
            panic!("{name} is required for RAID5; see docs/raid5/KERNEL_ENVIRONMENT.md")
        }))
        .canonicalize()
        .unwrap_or_else(|error| panic!("invalid {name}: {error}"))
    }

    for name in [
        "RUBLK_RAID5_UAPI_DIR",
        "RUBLK_RAID5_BTF",
        "RUBLK_RAID5_BPFTOOL",
        "RUBLK_RAID5_CLANG",
        "RUBLK_RAID5_CC",
    ] {
        println!("cargo:rerun-if-env-changed={name}");
    }
    assert_eq!(
        env::var("HOST").unwrap(),
        env::var("TARGET").unwrap(),
        "RAID5 currently requires a native Linux build; see docs/raid5/KERNEL_ENVIRONMENT.md"
    );
    let uapi = required_path("RUBLK_RAID5_UAPI_DIR");
    assert!(
        uapi.join("linux/io_uring.h").is_file(),
        "RUBLK_RAID5_UAPI_DIR must name the include directory produced by make headers_install"
    );
    let btf = required_path("RUBLK_RAID5_BTF");
    let out = PathBuf::from(env::var_os("OUT_DIR").unwrap());
    let cc = env::var_os("RUBLK_RAID5_CC").unwrap_or_else(|| "cc".into());
    let checked = Command::new(cc)
        .args(["-std=c11", "-Wall", "-Werror", "-I"])
        .arg(&uapi)
        .args(["-c", "scripts/raid5/check_abi.c", "-o"])
        .arg(out.join("raid5-check-abi.o"))
        .status()
        .expect("run C compiler for RAID5 UAPI checks");
    assert!(
        checked.success(),
        "kernel UAPI is incompatible with this RAID5 prototype; see docs/raid5/KERNEL_ENVIRONMENT.md"
    );

    let bpftool = env::var_os("RUBLK_RAID5_BPFTOOL").unwrap_or_else(|| "bpftool".into());
    let dumped = Command::new(bpftool)
        .args(["btf", "dump", "file"])
        .arg(&btf)
        .args(["format", "c"])
        .output()
        .expect("run bpftool to generate vmlinux.h from RUBLK_RAID5_BTF");
    assert!(
        dumped.status.success(),
        "bpftool BTF dump failed: {}",
        String::from_utf8_lossy(&dumped.stderr)
    );
    let header = String::from_utf8(dumped.stdout).expect("bpftool output must be UTF-8");
    for required in [
        "struct io_bpf_buf_desc {",
        "struct uring_bpf_data {",
        "struct bpf_iter_uring_buf {",
        "bpf_arena_alloc_pages(",
    ] {
        assert!(header.contains(required), "BTF dump lacks {required}; use the matching patched kernel and its bpftool (with kfunc prototype emission)");
    }
    fs::write(out.join("vmlinux.h"), header).expect("write generated vmlinux.h into OUT_DIR");
    let clang = env::var_os("RUBLK_RAID5_CLANG").unwrap_or_else(|| "clang".into());
    for name in ["probe", "kfixed", "xor"] {
        libbpf_cargo::SkeletonBuilder::new()
            .source(format!("src/raid5/bpf/{name}.bpf.c"))
            .clang(&clang)
            .clang_args([format!("-I{}", out.display())])
            .build_and_generate(out.join(format!("{name}.skel.rs")))
            .expect("build RAID5 BPF object and skeleton");
        println!("cargo:rerun-if-changed=src/raid5/bpf/{name}.bpf.c");
    }
    println!("cargo:rerun-if-changed=scripts/raid5/check_abi.c");
    println!("cargo:rerun-if-changed={}", uapi.display());
    println!("cargo:rerun-if-changed={}", btf.display());
}
