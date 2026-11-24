// SPDX-License-Identifier: GPL-2.0-or-later
#include <stddef.h>
#include <linux/io_uring.h>
_Static_assert(IORING_BPF_OP_SHIFT == 24, "BPF operation shift");
_Static_assert(IORING_OP_BPF == 65, "BPF opcode");
_Static_assert(IORING_SETUP_BPF_EXT == (1U << 21), "BPF setup flag");
_Static_assert(sizeof(struct io_bpf_buf_desc) == 16, "buf size");
_Static_assert(_Alignof(struct io_bpf_buf_desc) == 8, "buf align");
_Static_assert(offsetof(struct io_bpf_buf_desc, type) == 0, "type");
_Static_assert(offsetof(struct io_bpf_buf_desc, reserved) == 1, "reserved");
_Static_assert(offsetof(struct io_bpf_buf_desc, buf_index) == 2, "index");
_Static_assert(offsetof(struct io_bpf_buf_desc, len) == 4, "len");
_Static_assert(offsetof(struct io_bpf_buf_desc, addr) == 8, "addr");
_Static_assert(sizeof(struct io_uring_sqe) == 64, "SQE size");
_Static_assert(offsetof(struct io_uring_sqe, bpf_op_flags) == 28, "op flags");
int main(void) { return 0; }
