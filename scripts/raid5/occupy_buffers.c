// Test-only external buffer-table occupation; no target-side fault switch.
#define _GNU_SOURCE
#include <errno.h>
#include <linux/io_uring.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <unistd.h>
int main(int argc,char **argv){
 if(argc<5)return 2;
 int pid=atoi(argv[1]),depth=atoi(argv[2]);const char*release=argv[3];
 if(depth<=0||depth>256)return 2;
 int pfd=(int)syscall(SYS_pidfd_open,pid,0);if(pfd<0){perror("pidfd_open");return 2;}
 int *rings=calloc((size_t)argc,sizeof(int));struct iovec *vec=calloc((size_t)depth,sizeof(*vec));void *buf=NULL;
 if(!rings||!vec||posix_memalign(&buf,4096,4096))return 2;
 for(int i=0;i<depth;i++){vec[i].iov_base=buf;vec[i].iov_len=4096;}
 int nr=0;
 for(int i=4;i<argc;i++){
  int fd=(int)syscall(SYS_pidfd_getfd,pfd,atoi(argv[i]),0);if(fd<0){perror("pidfd_getfd");return 2;}
  struct io_uring_rsrc_update2 update={.offset=0,.data=(uint64_t)(uintptr_t)vec,.nr=(uint32_t)depth};
  long n=syscall(SYS_io_uring_register,fd,IORING_REGISTER_BUFFERS_UPDATE,&update,sizeof(update));
  if(n!=depth){fprintf(stderr,"occupy fd=%d n=%ld errno=%d\n",atoi(argv[i]),n,errno);return 2;}
  rings[nr++]=fd;
 }
 puts("HELD");fflush(stdout);
 for(int i=0;access(release,F_OK)&&i<3000;i++)usleep(10000);
 if(access(release,F_OK)){fprintf(stderr,"release timeout\n");return 2;}
 for(int i=0;i<depth;i++){vec[i].iov_base=NULL;vec[i].iov_len=0;}
 for(int i=0;i<nr;i++){
  struct io_uring_rsrc_update2 update={.offset=0,.data=(uint64_t)(uintptr_t)vec,.nr=(uint32_t)depth};
  long n=syscall(SYS_io_uring_register,rings[i],IORING_REGISTER_BUFFERS_UPDATE,&update,sizeof(update));
  if(n!=depth){fprintf(stderr,"release n=%ld errno=%d\n",n,errno);return 2;}close(rings[i]);
 }
 char done[4096];if(snprintf(done,sizeof(done),"%s.done",release)>=(int)sizeof(done))return 2;
 FILE*f=fopen(done,"wx");if(!f){perror("done");return 2;}fclose(f);
 free(buf);free(vec);free(rings);close(pfd);puts("RELEASED");return 0;
}
