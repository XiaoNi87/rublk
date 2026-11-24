fn main() {
    #[cfg(feature = "raid5")]
    {
        let out = std::path::PathBuf::from(std::env::var_os("OUT_DIR").unwrap());
        let abi = out.join("raid5-check-abi");
        let compiled = std::process::Command::new("cc")
            .args([
                "-Wall",
                "-Werror",
                "-Isrc/raid5/bpf/uapi",
                "scripts/raid5/check_abi.c",
                "-o",
            ])
            .arg(&abi)
            .status()
            .expect("compile pinned RAID5 ABI assertions");
        assert!(compiled.success(), "pinned C ABI mismatch");
        // All ABI assertions are compile-time; do not execute build-host binaries.
        println!("cargo:rerun-if-changed=scripts/raid5/check_abi.c");
        println!("cargo:rerun-if-changed=src/raid5/bpf/uapi");
        for name in ["probe", "kfixed", "xor"] {
            libbpf_cargo::SkeletonBuilder::new()
                .source(format!("src/raid5/bpf/{name}.bpf.c"))
                .clang_args(["-Isrc/raid5/bpf"])
                .build_and_generate(out.join(format!("{name}.skel.rs")))
                .expect("build RAID5 BPF capability probe");
            println!("cargo:rerun-if-changed=src/raid5/bpf/{name}.bpf.c");
        }
        println!("cargo:rerun-if-changed=src/raid5/bpf/vmlinux.h");
    }
}
