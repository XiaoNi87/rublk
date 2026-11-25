// SPDX-License-Identifier: GPL-2.0-or-later
// External test-only injector; never linked into the service.
#define _GNU_SOURCE
#include <sys/mman.h>
#include <dlfcn.h>
#include <errno.h>
#include <stdlib.h>
#include <stdatomic.h>
#include <unistd.h>
#include <stdio.h>
static _Atomic unsigned calls;
int madvise(void *addr, size_t len, int advice) {
 int (*real)(void *, size_t, int)=dlsym(RTLD_NEXT,"madvise");
 if (advice==MADV_POPULATE_WRITE && getenv("RAID5_TEST_POPULATE")) {
  unsigned n=atomic_fetch_add(&calls,1);
  if (getenv("RAID5_TEST_POPULATE")[0]=='a' || n==0) {
   char b[100];int z=snprintf(b,sizeof b,"INJECT_POPULATE_EFAULT call=%u bytes=%zu\n",n+1,len);write(2,b,z);errno=EFAULT;return -1;
  }
 }
 return real(addr,len,advice);
}
