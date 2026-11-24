// Independent whole-member parity checker; never linked into the target.
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/file.h>
#include <sys/ioctl.h>
#include <sys/stat.h>
#include <unistd.h>
#define C 65536u
#define PREFIX 1048576u
int main(int argc,char **argv) {
 if(argc!=4){fprintf(stderr,"three stopped members required\n");return 2;}
 int fd[3];uint64_t *buf[3],size[3];
 for(int d=0;d<3;d++) {
  fd[d]=open(argv[d+1],O_RDONLY|O_DIRECT|O_EXCL);if(fd[d]<0){perror("open");return 2;}
  if(flock(fd[d],LOCK_EX|LOCK_NB)){perror("ownership");return 2;}
  struct stat st;if(fstat(fd[d],&st)){perror("fstat");return 2;}
  if(S_ISBLK(st.st_mode)){if(ioctl(fd[d],0x80081272UL,&size[d])){perror("size");return 2;}}
  else size[d]=(uint64_t)st.st_size;
  if(posix_memalign((void**)&buf[d],4096,C)){return 2;}
 }
 if(size[0]!=size[1]||size[0]!=size[2]||size[0]<=PREFIX){return 2;}
 uint64_t stripes=(size[0]-PREFIX)/C;
 for(uint64_t s=0;s<stripes;s++) {
  for(int d=0;d<3;d++)if(pread(fd[d],buf[d],C,(off_t)(PREFIX+s*C))!=C){perror("short member read");return 2;}
  unsigned parity=2-s%3,a=(parity+1)%3,b=(parity+2)%3;
  for(unsigned i=0;i<C/sizeof(uint64_t);i++)if((buf[a][i]^buf[b][i])!=buf[parity][i]){
   fprintf(stderr,"parity mismatch stripe=%"PRIu64" byte=%u\n",s,i*8);return 1;
  }
 }
 for(int d=0;d<3;d++){free(buf[d]);close(fd[d]);}
 printf("{\"verified_stripes\":%"PRIu64",\"verified_backend_bytes\":%"PRIu64",\"parity_errors\":0}\n",stripes,stripes*C*3);
 return 0;
}
