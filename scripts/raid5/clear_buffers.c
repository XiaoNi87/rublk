/* SPDX-License-Identifier: GPL-2.0-or-later
 * Test-only: remove KFIXED table entries while RMW awaits an old-data read.
 * No fixed-buffer backend SQE is outstanding at this injection point.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <linux/io_uring.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <unistd.h>
int main(int argc,char **argv) {
 if(argc<4)return 2;
 int pid=atoi(argv[1]),depth=atoi(argv[2]);if(depth<=0||depth>256)return 2;
 int pfd=(int)syscall(SYS_pidfd_open,pid,0);if(pfd<0){perror("pidfd_open");return 2;}
 struct iovec *vec=calloc((size_t)depth,sizeof(*vec));if(!vec)return 2;
 for(int i=3;i<argc;i++) {
  int fd=(int)syscall(SYS_pidfd_getfd,pfd,atoi(argv[i]),0);if(fd<0){perror("pidfd_getfd");return 2;}
  struct io_uring_rsrc_update2 u={.offset=0,.data=(uint64_t)(uintptr_t)vec,.nr=(uint32_t)depth};
  long n=syscall(SYS_io_uring_register,fd,IORING_REGISTER_BUFFERS_UPDATE,&u,sizeof(u));
  if(n!=depth){fprintf(stderr,"clear n=%ld errno=%d\n",n,errno);close(fd);return 2;}
  close(fd);
 }
 free(vec);close(pfd);puts("CLEARED");return 0;
}
