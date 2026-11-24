/* SPDX-License-Identifier: GPL-2.0-or-later
 * Test-only FUSE backend fault controller, NOT a RAID5 copy/XOR fallback.
 * Proxy file storage intentionally exists outside the target under test.
 */
#define FUSE_USE_VERSION 31
#define _GNU_SOURCE
#include <fuse3/fuse.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdint.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>
static int files[3];
static atomic_int fired;
static const char *control;
struct handle {int member, fd;};
static struct handle *handle(struct fuse_file_info *fi) {return (struct handle *)(uintptr_t)fi->fh;}
static int member(const char *path) {
 if (strlen(path)==8 && !memcmp(path,"/member",7) && path[7]>='0' && path[7]<='2')return path[7]-'0';
 return -1;
}
static int fault(int m,const char *op,off_t offset) {
 if(atomic_load(&fired))return 0;
 FILE *f=fopen(control,"r");if(!f)return 0;
 int wanted;long long off;char operation[16],action[32];
 int n=fscanf(f,"%d %15s %lld %31s",&wanted,operation,&off,action);fclose(f);
 if(n!=4 || wanted!=m || strcmp(operation,op) || (off>=0 && off!=offset))return 0;
 if(atomic_exchange(&fired,1))return 0;
 char *hit=NULL,*release=NULL;
 if(asprintf(&hit,"%s.hit",control)<0 || asprintf(&release,"%s.release",control)<0)abort();
 int fd=open(hit,O_WRONLY|O_CREAT|O_EXCL|O_CLOEXEC,0600);if(fd>=0)close(fd);
 if(!strncmp(action,"hold-",5)) {
  unsigned i;for(i=0;i<30000 && access(release,F_OK);i++)usleep(1000);
  if(i==30000){free(hit);free(release);return 1;}
 }
 free(hit);free(release);
 if(strstr(action,"error"))return 1;
 if(strstr(action,"short"))return 2;
 return 0;
}
static int fs_getattr(const char *path,struct stat *st,struct fuse_file_info *fi) {
 (void)fi;memset(st,0,sizeof(*st));
 if(!strcmp(path,"/")){st->st_mode=S_IFDIR|0700;st->st_nlink=2;st->st_ino=1;return 0;}
 int m=member(path);if(m<0)return -ENOENT;
 if(fstat(files[m],st))return -errno;st->st_ino=11+m;return 0;
}
static int fs_readdir(const char *p,void *buf,fuse_fill_dir_t fill,off_t off,struct fuse_file_info *fi,enum fuse_readdir_flags flags) {
 (void)p;(void)off;(void)fi;(void)flags;
 fill(buf,".",NULL,0,0);fill(buf,"..",NULL,0,0);
 for(int i=0;i<3;i++){char name[16];snprintf(name,sizeof(name),"member%d",i);fill(buf,name,NULL,0,0);}return 0;
}
static int fs_open(const char *p,struct fuse_file_info *fi) {
 int m=member(p);if(m<0)return -ENOENT;
 struct handle *h=calloc(1,sizeof(*h));if(!h)return -ENOMEM;
 char path[64];snprintf(path,sizeof(path),"/proc/self/fd/%d",files[m]);
 h->fd=open(path,O_RDWR|O_CLOEXEC);if(h->fd<0){int e=errno;free(h);return -e;}
 h->member=m;fi->fh=(uintptr_t)h;fi->direct_io=1;return 0;
}
static int fs_release(const char *p,struct fuse_file_info *fi) {(void)p;struct handle *h=handle(fi);close(h->fd);free(h);return 0;}
static int fs_read(const char *p,char *buf,size_t n,off_t off,struct fuse_file_info *fi) {
 (void)p;int m=handle(fi)->member;int f=fault(m,"read",off);if(f)return f==1?-EIO:0;
 ssize_t r=pread(handle(fi)->fd,buf,n,off);return r<0?-errno:(int)r;
}
static int fs_write(const char *p,const char *buf,size_t n,off_t off,struct fuse_file_info *fi) {
 (void)p;int m=handle(fi)->member;int f=fault(m,"write",off);if(f)return f==1?-EIO:0;
 ssize_t r=pwrite(handle(fi)->fd,buf,n,off);return r<0?-errno:(int)r;
}
static int fs_fsync(const char *p,int data,struct fuse_file_info *fi) {
 (void)p;int m=handle(fi)->member;if(fault(m,"fsync",-1))return -EIO;
 int r=data?fdatasync(handle(fi)->fd):fsync(handle(fi)->fd);return r<0?-errno:0;
}
static int fs_flock(const char *p,struct fuse_file_info *fi,int op) {(void)p;return flock(handle(fi)->fd,op)<0?-errno:0;}
static void *fs_init(struct fuse_conn_info *conn,struct fuse_config *cfg) {
 conn->want &= ~FUSE_CAP_WRITEBACK_CACHE;
 cfg->direct_io=1;cfg->kernel_cache=0;cfg->use_ino=1;cfg->entry_timeout=0;cfg->attr_timeout=0;cfg->negative_timeout=0;return NULL;
}
int main(int argc,char **argv) {
 if(argc!=4){fprintf(stderr,"mountpoint backing-directory fault-control\n");return 2;}
 for(int i=0;i<3;i++){char *p=NULL;if(asprintf(&p,"%s/member%d",argv[2],i)<0)abort();files[i]=open(p,O_RDWR|O_CLOEXEC);free(p);if(files[i]<0){perror("backend open");return 2;}}
 control=argv[3];struct fuse_operations ops={.getattr=fs_getattr,.readdir=fs_readdir,.open=fs_open,.release=fs_release,.read=fs_read,.write=fs_write,.fsync=fs_fsync,.flock=fs_flock,.init=fs_init};
 char *args[]={argv[0],"-f","-s",argv[1],NULL};
 int count=4;
 if(getenv("RAID5_FAULT_FS_MULTITHREAD")){args[2]=argv[1];args[3]=NULL;count=3;}
 int result=fuse_main(count,args,&ops,NULL);
 for(int i=0;i<3;i++)close(files[i]);return result;
}
