// SPDX-License-Identifier: GPL-2.0
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
char LICENSE[] SEC("license") = "GPL";
struct {
    __uint(type, BPF_MAP_TYPE_ARRAY);
    __uint(max_entries, 1);
    __type(key, __u32);
    __type(value, __u8[64]);
} output SEC(".maps");
extern void uring_bpf_set_result(struct uring_bpf_data *, int) __ksym;
SEC("struct_ops/probe_prep")
int BPF_PROG(probe_prep, struct uring_bpf_data *data, const struct io_uring_sqe *sqe)
{
    return sqe->len == 1 ? 0 : -22;
}
SEC("struct_ops/probe_issue")
int BPF_PROG(probe_issue, struct uring_bpf_data *data, unsigned int flags)
{
    __u32 key = 0;
    __u8 *p = bpf_map_lookup_elem(&output, &key);
    if (!p) { uring_bpf_set_result(data, -2); return 0; }
    for (int i = 0; i < 64; i++) p[i] = i ^ 0xa5;
    uring_bpf_set_result(data, 64);
    return 0;
}
SEC("struct_ops/probe_fail")
void BPF_PROG(probe_fail, struct uring_bpf_data *data) {}
SEC("struct_ops/probe_cleanup")
void BPF_PROG(probe_cleanup, struct uring_bpf_data *data) {}
SEC(".struct_ops.link")
struct uring_bpf_ops probe_ops = {
    .id = 0,
    .prep_fn = (void *)probe_prep,
    .issue_fn = (void *)probe_issue,
    .fail_fn = (void *)probe_fail,
    .cleanup_fn = (void *)probe_cleanup,
};
