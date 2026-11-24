// SPDX-License-Identifier: GPL-2.0
#include "vmlinux.h"
#include <bpf/bpf_helpers.h>
#include <bpf/bpf_tracing.h>
char LICENSE[] SEC("license") = "GPL";
#if defined(__BPF_FEATURE_ADDR_SPACE_CAST)
#define __arena __attribute__((address_space(1)))
#define cast_kern(ptr)
#else
#define __arena
#define cast_kern(ptr) bpf_addr_space_cast(ptr, 0, 1)
#endif
#define MAX_LEN 65536
#define MAX_ARENA_BYTES 67108864
const volatile __u32 arena_bytes = 196608;
struct xor_desc { __u32 nr; __u32 len; __u32 output; __u32 reserved;
    struct io_bpf_buf_desc source[3]; };
struct { __uint(type, BPF_MAP_TYPE_ARENA); __uint(map_flags, BPF_F_MMAPABLE);
    __uint(max_entries, 48); } arena SEC(".maps");
unsigned char __arena *arena_base;
extern void uring_bpf_set_result(struct uring_bpf_data *, int) __ksym;
extern int bpf_iter_uring_buf_new(struct bpf_iter_uring_buf *, struct uring_bpf_data *, struct io_bpf_buf_desc *, int) __ksym;
extern int *bpf_iter_uring_buf_next(struct bpf_iter_uring_buf *) __ksym;
extern void bpf_iter_uring_buf_destroy(struct bpf_iter_uring_buf *) __ksym;
extern int bpf_uring_buf_dynptr(struct bpf_iter_uring_buf *, struct bpf_dynptr *) __ksym;
extern void *bpf_dynptr_slice(const struct bpf_dynptr *, __u64, void *, __u64) __ksym;
struct walk {
    struct bpf_iter_uring_buf *iter;
    unsigned char __arena *dst;
    __u32 len,total;
    int error;
    bool first;
};
static __noinline void accumulate(__u64 *src, __u64 __arena *out, bool first) {
    for (int i=0;i<64;i++) {
        if (first) out[i]=src[i];
        else out[i]^=src[i];
    }
}
/* Walk one source completely, so mismatched page boundaries cannot truncate
 * another source. Accumulation stays entirely in the BPF arena. */
static int xor_source(__u32 n, struct walk *c) {
    if (c->total>=c->len) return 1;
    int *avail=bpf_iter_uring_buf_next(c->iter);
    if (!avail || *avail<512 || *avail>4096 || (*avail&511)) {c->error=-5;return 1;}
    int len=*avail;
    if (len>c->len-c->total) {c->error=-5;return 1;}
    struct bpf_dynptr ptr;
    if (bpf_uring_buf_dynptr(c->iter,&ptr)) {c->error=-5;return 1;}
    for (int off=0;off<4096;off+=512) {
        if (off>=len) break;
        __u64 *src=bpf_dynptr_slice(&ptr,off,NULL,512);
        if (!src) {c->error=-5;return 1;}
        __u64 __arena *dst=(void __arena *)(c->dst+c->total);
        accumulate(src,dst,c->first);
        c->total+=512;
    }
    return 0;
}
SEC("struct_ops/xor_prep")
int BPF_PROG(xor_prep, struct uring_bpf_data *data, const struct io_uring_sqe *sqe) {
    if (sqe->len!=1) return -22;
    *(__u64 *)data->pdu=sqe->addr;
    return 0;
}
SEC("struct_ops.s/xor_issue")
int BPF_PROG(xor_issue, struct uring_bpf_data *data) {
    struct xor_desc d;
    struct bpf_iter_uring_buf iter;
    /* issue_fn is sleepable; a valid, retained descriptor may need a page
     * fault. The nofault probe helper can reject such an address. */
    int ret=bpf_copy_from_user(&d,sizeof(d),(void *)*(__u64 *)data->pdu);
    if (ret) goto done;
    if ((d.nr!=2 && d.nr!=3) || !d.len || d.len>MAX_LEN || (d.len&511) ||
        d.reserved || (d.output&511) || arena_bytes<d.len || arena_bytes>MAX_ARENA_BYTES || d.output>arena_bytes-d.len) {ret=-22;goto done;}
    #define VALID(I) ((d.source[I].type==IO_BPF_BUF_USER || d.source[I].type==IO_BPF_BUF_KFIXED) && \
        !d.source[I].reserved && d.source[I].len==d.len && !(d.source[I].addr&511) && \
        (d.source[I].type!=IO_BPF_BUF_USER || !d.source[I].buf_index) && \
        d.source[I].addr<=~0ull-(__u64)d.len)
    if (!VALID(0) || !VALID(1) || (d.nr==3 && !VALID(2))) {ret=-22;goto done;}
    unsigned char __arena *base=arena_base;
    bpf_arena_alloc_pages(&arena,NULL,0,0,0);
    if (!base) {ret=-12;goto done;}
    cast_kern(base);
    struct walk c={.iter=&iter,.dst=base+d.output,.len=d.len,.first=true};
    ret=bpf_iter_uring_buf_new(&iter,data,&d.source[0],1);
    if (!ret) {
        ret=bpf_loop(128,xor_source,&c,0);
        if(ret>=0)ret=c.error?c.error:(c.total==d.len?0:-5);
    }
    bpf_iter_uring_buf_destroy(&iter);
    if (ret) goto done;
    c.total=0;c.first=false;
    ret=bpf_iter_uring_buf_new(&iter,data,&d.source[1],1);
    if (!ret) {
        ret=bpf_loop(128,xor_source,&c,0);
        if(ret>=0)ret=c.error?c.error:(c.total==d.len?0:-5);
    }
    bpf_iter_uring_buf_destroy(&iter);
    if (ret) goto done;
    if (d.nr==3) {
        c.total=0;
        ret=bpf_iter_uring_buf_new(&iter,data,&d.source[2],1);
        if (!ret) {
            ret=bpf_loop(128,xor_source,&c,0);
            if(ret>=0)ret=c.error?c.error:(c.total==d.len?0:-5);
        }
        bpf_iter_uring_buf_destroy(&iter);
        if (ret) goto done;
    }
    ret=d.len;
done:
    uring_bpf_set_result(data,ret);
    return 0;
}
SEC("struct_ops/xor_fail")
void BPF_PROG(xor_fail, struct uring_bpf_data *data) {}
SEC("struct_ops/xor_cleanup")
void BPF_PROG(xor_cleanup, struct uring_bpf_data *data) {}
SEC(".struct_ops.link")
struct uring_bpf_ops xor_ops={.id=0,.prep_fn=(void *)xor_prep,.issue_fn=(void *)xor_issue,
    .fail_fn=(void *)xor_fail,.cleanup_fn=(void *)xor_cleanup};
