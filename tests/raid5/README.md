# RAID5 module tests

RAID5 tests live under `tests/`, alongside the repository's existing tests.
The files in this subdirectory contain module-level tests, loaded through
`#[cfg(test)]` declarations in `src/raid5/`. This preserves access to private
state without exporting implementation details or compiling test code into
normal builds. Run these 37 tests with:

```sh
cargo test --locked --no-default-features --features raid5 --lib
```

The top-level `tests/raid5_abi.rs`, `tests/raid5_coordination.rs` and
`tests/raid5_ring.rs` are separate integration tests:

```sh
cargo test --locked --no-default-features --features raid5 \
    --test raid5_abi --test raid5_coordination --test raid5_ring
```

Two ring tests are explicitly ignored by default because they require root
and the compatible kernel. See their annotations before running `--ignored`.
All RAID5 builds/tests use the external kernel build inputs described in
[the environment guide](../../docs/raid5/KERNEL_ENVIRONMENT.md).
