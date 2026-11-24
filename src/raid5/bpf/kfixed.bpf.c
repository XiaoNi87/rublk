// SPDX-License-Identifier: GPL-2.0
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
char LICENSE[] SEC("license") = "GPL";
struct { __uint(type, BPF_MAP_TYPE_ARRAY); __uint(max_entries, 1);
    __type(key, __u32); __type(value, __u8[64]); } output SEC(".maps");
extern void uring_bpf_set_result(struct uring_bpf_data *, int) __ksym;
extern int bpf_iter_uring_buf_new(struct bpf_iter_uring_buf *, struct uring_bpf_data *, struct io_bpf_buf_desc *, int) __ksym;
extern int *bpf_iter_uring_buf_next(struct bpf_iter_uring_buf *) __ksym;
extern void bpf_iter_uring_buf_destroy(struct bpf_iter_uring_buf *) __ksym;
extern int bpf_uring_buf_dynptr(struct bpf_iter_uring_buf *, struct bpf_dynptr *) __ksym;
SEC("struct_ops/kfixed_prep")
int BPF_PROG(kfixed_prep, struct uring_bpf_data *data, const struct io_uring_sqe *sqe)
{
    if (sqe->len != 1) return -22;
    *(__u64 *)data->pdu = sqe->addr;
    return 0;
}
SEC("struct_ops.s/kfixed_issue")
int BPF_PROG(kfixed_issue, struct uring_bpf_data *data, unsigned int flags)
{
    struct io_bpf_buf_desc desc;
    struct bpf_iter_uring_buf iter;
    struct bpf_dynptr ptr;
    __u32 key = 0;
    int ret = bpf_probe_read_user(&desc, sizeof(desc), (void *)*(__u64 *)data->pdu);
    if (ret) goto done;
    if (desc.type != IO_BPF_BUF_KFIXED || desc.reserved || desc.len != 64) {ret = -22; goto done;}
    ret = bpf_iter_uring_buf_new(&iter, data, &desc, 1);
    if (ret) goto destroy;
    if (!bpf_iter_uring_buf_next(&iter)) {ret = -5; goto destroy;}
    ret = bpf_uring_buf_dynptr(&iter, &ptr);
    if (ret) goto destroy;
    __u8 *out = bpf_map_lookup_elem(&output, &key);
    if (!out) {ret = -2; goto destroy;}
    ret = bpf_dynptr_read(out, 64, &ptr, 0, 0);
    if (!ret) ret = 64;
destroy:
    bpf_iter_uring_buf_destroy(&iter);
done:
    uring_bpf_set_result(data, ret);
    return 0;
}
SEC("struct_ops/kfixed_fail")
void BPF_PROG(kfixed_fail, struct uring_bpf_data *data) {}
SEC("struct_ops/kfixed_cleanup")
void BPF_PROG(kfixed_cleanup, struct uring_bpf_data *data) {}
SEC(".struct_ops.link")
struct uring_bpf_ops kfixed_ops = {.id = 0, .prep_fn = (void *)kfixed_prep,
    .issue_fn = (void *)kfixed_issue, .fail_fn = (void *)kfixed_fail,
    .cleanup_fn = (void *)kfixed_cleanup};
