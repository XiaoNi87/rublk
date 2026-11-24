# Experimental dependency forks

rublk no longer contains copies of io-uring or libublk. Cargo downloads the
independent forks below using HTTPS, pinned to immutable full commit IDs.
The versions are intentionally the same as those used by the prototype;
this migration is not an upgrade to either project's current main branch.

| Crate | Version | Upstream base | Experimental revision |
|---|---|---|---|
| io-uring | 0.7.12 | 44c9320214fdcb27ac8cc44189f7ef0e89bdde87 | 18c637802eea403498c36df74ff54ff96584d795 |
| libublk | 0.4.7 | 4687c539c1d43f535793c5df601533e37721094e | 7069ae7affd7dd5cbdda7fc0b903e7e4a6bff869 |

## Repositories and patch boundaries

- https://github.com/XiaoNi87/io-uring/tree/raid5-bpf
  adds opt-in `raid5-bpf` descriptors, the BPF SQE builder and the custom-ring
  setup flag. The package's upstream source, workspace, examples, tests and
  license remain in that independent repository.
- https://github.com/XiaoNi87/libublk-rs/tree/raid5-bpf
  adds the BPF-capable task-ring initializer, terminal error completion,
  cancellable device startup and single-attempt recovery takeover. BPF support
  is opt-in. The published registry `libublk-rs-sys` 0.1.1 dependency is retained
  to match the original prototype instead of switching to a different local
  sys-crate snapshot.

These patches target custom kernel commit
`42abd4d4c48750e82cb24f52d23ab5c07b2d81c9`. They are not claims of a stable
upstream kernel ABI or accepted upstream Rust APIs. See KERNEL_ENVIRONMENT.md
for the external kernel/UAPI/BTF prerequisites.

## How Cargo selects them

The root `[patch.crates-io]` table points each crate at its Git repository and
full `rev`. This covers both the direct io-uring dependency and libublk's
compatible transitive dependency, so they use one patched io-uring 0.7.12.
The root Cargo.lock records the same Git source identities; `--locked` is used
for reproducible builds. Another transitive dependency may use io-uring 0.5.13;
that older registry dependency remains untouched and is not the RAID5 path.

Do not replace `rev` with an unpinned moving branch, upgrade to the forks'
main/master branches, or update unrelated lockfile versions while reviewing
this change. The source baselines are deliberately older than today's upstream
heads. A migration to newer APIs needs its own review and validation.

To inspect the effective dependencies:

```sh
cargo tree --locked -i io-uring@0.7.12
cargo tree --locked -i libublk@0.4.7
```

The initial fetch needs network access to GitHub/crates.io. Later builds can
use the populated Cargo cache with `--offline --locked`. Keep the root lockfile.
No manual copying of dependency sources into this repository is needed.

## Working on the dependencies independently

Each fork has a `raid5-bpf` branch based on the upstream base in the table.
io-uring's experimental interface is one commit. libublk separates the queue/
ring changes from the control-start/recovery changes. No blanket upstream
version upgrade or whitespace cleanup is part of those patches.

For libublk's own build with `raid5-bpf`, its Cargo invocation must also select
the matching io-uring patch, because `[patch]` is controlled by the invoking
root project and is not inherited from an unrelated rublk checkout:

```sh
cargo check --lib --features raid5-bpf \
  --config 'patch.crates-io.io-uring.git="https://github.com/XiaoNi87/io-uring.git"' \
  --config 'patch.crates-io.io-uring.rev="18c637802eea403498c36df74ff54ff96584d795"'
```

Normal dependency library builds do not require copying the experimental
kernel headers into their source trees. Running experimental operations still
requires the compatible kernel. BPF program compilation in rublk additionally
requires the exported UAPI and BTF described in KERNEL_ENVIRONMENT.md.

Once suitable interfaces are accepted and released upstream, evaluate switching
back to release dependencies in a separate change, then remove the temporary
Git patch entries. Do not silently drop required APIs before that transition.

## Validation scope

The migration preserves the previously tested versions and runtime source
logic, but Git source identities differ from the original frozen binary.
Compile/unit/probe validation must be rerun for the new dependency layout.
Earlier performance and persistence evidence is historical; it does not prove
that this newly rebuilt binary has passed another full device acceptance run.
