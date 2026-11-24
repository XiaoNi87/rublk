/* SPDX-License-Identifier: GPL-2.0-or-later
 * External test-only tracer: invalidate one WRITE COMMIT command before entry
 * into io_uring_enter. No production fault hook, no synthetic CQE replacement.
 * Harness restricts this to disposable file-backed ublk test devices.
 * Counts the rejected request's 4KiB writes at backend offset 1MiB;
 * peer-stripe writes are deliberately excluded from that replay count.
 */
#define _GNU_SOURCE
#if defined(__x86_64__)
#include <sys/user.h>
#endif
#include <sys/ptrace.h>
#include <linux/ptrace.h>
#include <linux/io_uring.h>
#include <sys/syscall.h>
#include <sys/uio.h>
#include <sys/wait.h>
#include <signal.h>
#include <stdint.h>
#include <stddef.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <errno.h>
#include <time.h>
#include <elf.h>

#define MAXFD 4096
#define MAXTID 256
struct ring {
    int valid, qid, qid_known;
    uint64_t backend_data;
    struct io_uring_params params;
    uint64_t sq, cq, sqes;
};
struct thread { pid_t tid; uint64_t nr, args[6]; int entered; };
static struct ring rings[MAXFD];
static struct thread threads[MAXTID];
static pid_t child;
static uint64_t iod_base, writes, writes_before, injected_data;
static int injected, negative_seen, injected_fd = -1;
static int fetch_mode, fetch_all, hold_mode;
static const char *bpf_fault;
static int bpf_background;
/* Notification fault modes are externally armed, never production hooks. */
static const char *notify_mode, *notify_arm;
static int notify_sync, notify_fd=-1, notify_dup=-1, notify_signalled;
static int notify_ring=-1, notify_read_pending, notify_read_seen;
static void arm_notification(void) {
    if(notify_mode && !notify_signalled && notify_dup>=0 && !access(notify_arm,F_OK)) {
        uint64_t one=1;
        if(write(notify_dup,&one,8)!=8){perror("signal traced eventfd");exit(2);}
        notify_signalled=1;printf("NOTIFICATION_ARMED fd=%d ring=%d\n",notify_fd,notify_ring);fflush(stdout);
    }
}
static struct { int fd, seen; uint64_t data; } faults[256];
static pid_t held_tid, held_owner;
static int held_once, released, drained, held_fd;
static uint64_t held_cq;
static uint32_t held_index, held_head_offset;
static uint64_t release_at, held_at;
static uint64_t clock_ns(void) { struct timespec t; clock_gettime(CLOCK_MONOTONIC,&t); return (uint64_t)t.tv_sec*1000000000ULL+t.tv_nsec; }

static void die(const char *what) { perror(what); exit(2); }
static void read_user(uint64_t addr, void *buf, size_t n) {
    struct iovec local={buf,n}, remote={(void *)(uintptr_t)addr,n};
    if(process_vm_readv(child,&local,1,&remote,1,0)!=(ssize_t)n){fprintf(stderr,"READ_FAIL child=%d address=%llx bytes=%zu\n",child,(unsigned long long)addr,n);die("read child memory");}
}
static uint32_t u32(uint64_t addr) { uint32_t v; read_user(addr,&v,sizeof(v)); return v; }
static struct thread *thread(pid_t tid) {
    for(unsigned i=0;i<MAXTID;i++)if(threads[i].tid==tid)return &threads[i];
    for(unsigned i=0;i<MAXTID;i++)if(!threads[i].tid){threads[i].tid=tid;return &threads[i];}
    errno=E2BIG;die("too many traced threads");return NULL;
}
static void inspect_submit(int fd, uint32_t count) {
    if(fd<0 || fd>=MAXFD || !rings[fd].valid || !count)return;
    struct ring *r=&rings[fd];
    if(!r->sq || !r->sqes)return;
    if(r->params.flags & IORING_SETUP_SQPOLL){errno=EINVAL;die("SQPOLL unsupported by test");}
    uint32_t head=u32(r->sq+r->params.sq_off.head);
    uint32_t mask=u32(r->sq+r->params.sq_off.ring_mask);
    if(count>r->params.sq_entries){errno=E2BIG;die("submission count");}
    for(uint32_t i=0;i<count;i++) {
        uint32_t index=(head+i)&mask;
        if(!(r->params.flags & (1U<<16))) /* IORING_SETUP_NO_SQARRAY */
            index=u32(r->sq+r->params.sq_off.array+index*4ULL);
        uint64_t addr=r->sqes+index*((r->params.flags & IORING_SETUP_SQE128)?128ULL:64ULL);
        struct io_uring_sqe sqe;read_user(addr,&sqe,sizeof(sqe));
        if(sqe.opcode==IORING_OP_URING_CMD && (sqe.cmd_op & 255)==0x20) {
            uint16_t qid;memcpy(&qid,((char *)&sqe)+48,sizeof(qid));
            r->qid=qid;r->qid_known=1;
        }
        if(hold_mode && r->qid_known && r->qid==1 &&
           sqe.opcode==IORING_OP_WRITE_FIXED && sqe.off==1114112 && sqe.len==4096)
            r->backend_data=sqe.user_data;
        if((sqe.opcode==IORING_OP_WRITE_FIXED || sqe.opcode==IORING_OP_WRITE) &&
           (sqe.flags & IOSQE_FIXED_FILE) && sqe.off==1048576 && sqe.len==4096)writes++;
        if(notify_mode) {
            if(iod_base && sqe.opcode==IORING_OP_POLL_ADD &&
               (notify_sync ? !r->qid_known : (r->qid_known && r->qid==0))) {
                if(notify_fd<0) {
                    notify_fd=sqe.fd;notify_ring=fd;
                    int pidfd=syscall(SYS_pidfd_open,child,0);
                    if(pidfd<0)die("pidfd_open");
                    notify_dup=syscall(SYS_pidfd_getfd,pidfd,notify_fd,0);close(pidfd);
                    if(notify_dup<0)die("pidfd_getfd notification");
                    printf("NOTIFICATION_SELECTED fd=%d ring=%d sync=%d\n",notify_fd,fd,notify_sync);fflush(stdout);
                }
                if(!injected && notify_signalled && !strcmp(notify_mode,"poll") && fd==notify_ring) {
                    int invalid=-1;struct iovec l={&invalid,4},remote={(void *)(uintptr_t)(addr+offsetof(struct io_uring_sqe,fd)),4};
                    if(process_vm_writev(child,&l,1,&remote,1,0)!=4)die("invalidate PollAdd fd");
                    faults[0].fd=fd;faults[0].data=sqe.user_data;injected=1;
                    printf("INJECT_NOTIFICATION_POLL fd=%d\n",fd);fflush(stdout);
                }
            }
            continue;
        }
        if(bpf_fault) {
            if(!injected && iod_base && (bpf_background ? !r->qid_known : r->qid_known) && sqe.opcode==65) {
                uint32_t nr=u32(sqe.addr), len=512;
                if(nr<2 || nr>3){errno=EINVAL;die("BPF descriptor source count");}
                uint64_t addresses[4]; unsigned n=0;
                if(!strcmp(bpf_fault,"short")) {
                    addresses[n++]=sqe.addr+4;
                    for(unsigned j=0;j<nr;j++)addresses[n++]=sqe.addr+16+16*j+4;
                    for(unsigned j=0;j<n;j++) {
                        struct iovec l={&len,4},remote={(void *)(uintptr_t)addresses[j],4};
                        if(process_vm_writev(child,&l,1,&remote,1,0)!=4)die("short BPF descriptor");
                    }
                } else {
                    uint64_t invalid=0;
                    struct iovec l={&invalid,8},remote={(void *)(uintptr_t)(sqe.addr+24),8};
                    if(process_vm_writev(child,&l,1,&remote,1,0)!=8)die("invalid USER source");
                }
                faults[0].fd=fd;faults[0].data=sqe.user_data;injected=1;
                printf("INJECT_BPF_DESCRIPTOR mode=%s fd=%d\n",bpf_fault,fd);fflush(stdout);
            }
            continue;
        }
        if((injected && !fetch_all) || !iod_base || sqe.opcode!=IORING_OP_URING_CMD ||
           (sqe.cmd_op & 255)!=(fetch_mode ? 0x20U : 0x21U))continue;
        struct {uint16_t qid,tag;int32_t result;uint64_t addr;} cmd;
        memcpy(&cmd,((char *)&sqe)+48,sizeof(cmd));
        if(!fetch_all && (cmd.qid || (cmd.tag && !hold_mode && !getenv("RAID5_FAULT_ANY_TAG"))))continue;
        if(!fetch_mode && (cmd.result!=4096 || writes!=2))continue;
        /* q0/tag0 is the sole request owner. Exactly two 4KiB backend write
         * SQEs precede its successful WRITE result. The ublk IOD PFN mapping
         * deliberately cannot be read with process_vm_readv(). */
        uint32_t invalid=0;
        struct iovec local={&invalid,sizeof(invalid)};
        struct iovec remote={(void *)(uintptr_t)(addr+offsetof(struct io_uring_sqe,cmd_op)),sizeof(invalid)};
        if(process_vm_writev(child,&local,1,&remote,1,0)!=(ssize_t)sizeof(invalid))die("patch command");
        if(injected>=256){errno=E2BIG;die("fault records");}
        faults[injected].fd=fd;faults[injected].data=sqe.user_data;
        injected++;writes_before=writes;injected_data=sqe.user_data;injected_fd=fd;
        printf("INJECT_COMMIT fd=%d command=%u userdata=%llu prior_write_sqes=%llu\n",
               fd,sqe.cmd_op,(unsigned long long)injected_data,(unsigned long long)writes_before);
        fflush(stdout);
    }
}
static void inspect_cq(int fd) {
    if(fd<0 || fd>=MAXFD)return;
    struct ring *r=&rings[fd];if(!r->valid || !r->cq)return;
    uint32_t head=u32(r->cq+r->params.cq_off.head),tail=u32(r->cq+r->params.cq_off.tail);
    uint32_t mask=u32(r->cq+r->params.cq_off.ring_mask);
    for(uint32_t i=head;i!=tail && i-head<r->params.cq_entries;i++) {
        struct io_uring_cqe cqe;
        uint64_t addr=r->cq+r->params.cq_off.cqes+(i&mask)*((r->params.flags & IORING_SETUP_CQE32)?32ULL:16ULL);
        read_user(addr,&cqe,sizeof(cqe));
        for(int j=0;j<injected;j++)if(!faults[j].seen && faults[j].fd==fd &&
            cqe.user_data==faults[j].data && cqe.res==(notify_mode ? -EBADF : bpf_fault ? (!strcmp(bpf_fault,"short") ? 512 : -EIO) : -EINVAL)) {
            faults[j].seen=1;negative_seen++;
            printf(notify_mode ? "NEGATIVE_NOTIFICATION_CQE res=%d fd=%d userdata=%llu\n" : bpf_fault ? "OBSERVED_BPF_CQE res=%d fd=%d userdata=%llu\n" : "NEGATIVE_COMMAND_CQE res=%d fd=%d userdata=%llu\n",cqe.res,fd,(unsigned long long)cqe.user_data);
            fflush(stdout);
        }
        if(hold_mode && !held_once && r->qid_known && r->qid==1 &&
           cqe.user_data==r->backend_data && cqe.res==4096) {
            held_once=1;held_fd=fd;held_cq=r->cq;
            held_index=i;held_head_offset=r->params.cq_off.head;
            printf("HOLD_ORIGINAL_BACKEND_CQE fd=%d userdata=%llu res=4096\n",fd,(unsigned long long)cqe.user_data);
            fflush(stdout);
        }
    }
}
static void enter(struct thread *t, const struct ptrace_syscall_info *info) {
    if(released && !drained && rings[held_fd].qid_known && t->tid==held_owner) {
        uint32_t head=u32(held_cq+held_head_offset);
        if((int32_t)(head-held_index)>0) {
            drained=1;printf("ORIGINAL_BACKEND_CQE_CONSUMED after_release=1\n");fflush(stdout);
        }
    }
    t->nr=info->entry.nr;memcpy(t->args,info->entry.args,sizeof(t->args));t->entered=1;
    if(notify_mode && notify_signalled && !injected && !strcmp(notify_mode,"drain") &&
       t->nr==SYS_read && (int)t->args[0]==notify_fd) {
        /* Change the native read fd, so the kernel returns real EBADF. */
#if defined(__x86_64__)
        struct user_regs_struct regs;
#elif defined(__aarch64__)
        uint64_t regs[34];
#else
#error "notification drain injection needs native register support"
#endif
        struct iovec io={&regs,sizeof(regs)};
        if(ptrace(PTRACE_GETREGSET,t->tid,(void *)NT_PRSTATUS,&io))die("get drain registers");
        if(io.iov_len!=sizeof(regs)){errno=EINVAL;die("drain register size");}
#if defined(__x86_64__)
        regs.rdi=(uint64_t)-1;
#else
        regs[0]=(uint64_t)-1;
#endif
        if(ptrace(PTRACE_SETREGSET,t->tid,(void *)NT_PRSTATUS,&io))die("set drain registers");
        injected=1;notify_read_pending=t->tid;
        printf("INJECT_NOTIFICATION_DRAIN fd=%d tid=%d\n",notify_fd,t->tid);fflush(stdout);
    }
    if(t->nr==SYS_io_uring_enter)inspect_submit((int)t->args[0],(uint32_t)t->args[1]);
}
static void leave(struct thread *t, const struct ptrace_syscall_info *info) {
    if(!t->entered)return;t->entered=0;
    int64_t result=info->exit.rval;
    if(notify_read_pending==t->tid && t->nr==SYS_read) {
        if(result!=-EBADF){errno=EINVAL;die("drain expected EBADF");}
        notify_read_pending=0;notify_read_seen=1;negative_seen++;
        printf("NEGATIVE_NOTIFICATION_READ res=%lld\n",(long long)result);fflush(stdout);
    }
    if(t->nr==SYS_io_uring_setup && result>=0) {
        if(result>=MAXFD){errno=E2BIG;die("ring fd");}
        struct ring *r=&rings[result];memset(r,0,sizeof(*r));r->valid=1;
        read_user(t->args[1],&r->params,sizeof(r->params));
    } else if(t->nr==SYS_mmap && !info->exit.is_error) {
        int fd=(int)t->args[4];uint64_t offset=t->args[5];
        if(fd>=0 && fd<MAXFD && rings[fd].valid) {
            struct ring *r=&rings[fd];
            if(offset==IORING_OFF_SQ_RING) {
                r->sq=result;
                if(r->params.features & IORING_FEAT_SINGLE_MMAP)r->cq=result;
            } else if(offset==IORING_OFF_CQ_RING)r->cq=result;
            else if(offset==IORING_OFF_SQES)r->sqes=result;
        }
        char path[128],target[256];snprintf(path,sizeof(path),"/proc/%d/fd/%d",child,fd);
        ssize_t n=readlink(path,target,sizeof(target)-1);
        if(n>0){target[n]=0;if(!strncmp(target,"/dev/ublkc",10) && offset==0)iod_base=result;}
    } else if(t->nr==SYS_io_uring_enter)inspect_cq((int)t->args[0]);
}
int main(int argc,char **argv) {
    if(argc<3){fprintf(stderr,"command_fault pid-file command [args...]\n");return 2;}
    notify_mode=getenv("RAID5_FAULT_NOTIFICATION");
    notify_arm=getenv("RAID5_FAULT_NOTIFICATION_ARM");
    notify_sync=getenv("RAID5_FAULT_NOTIFICATION_SYNC")!=NULL;
    if(notify_mode && (!notify_arm || (strcmp(notify_mode,"poll") && strcmp(notify_mode,"drain"))))return 2;
    bpf_fault=getenv("RAID5_FAULT_BPF_DESCRIPTOR");
    bpf_background=getenv("RAID5_FAULT_BPF_BACKGROUND")!=NULL;
    hold_mode=getenv("RAID5_FAULT_HOLD")!=NULL;
    fetch_mode=getenv("RAID5_FAULT_FETCH")!=NULL;
    fetch_all=getenv("RAID5_FAULT_FETCH_ALL")!=NULL;
    child=fork();if(child<0)die("fork");
    if(!child){if(ptrace(PTRACE_TRACEME,0,0,0))die("TRACEME");raise(SIGSTOP);execv(argv[2],argv+2);die("exec");}
    FILE *pid=fopen(argv[1],"w");if(!pid)die("pid-file");fprintf(pid,"%d\n",child);fclose(pid);
    int status;if(waitpid(child,&status,0)!=child || !WIFSTOPPED(status))die("initial stop");
    long options=PTRACE_O_TRACESYSGOOD|PTRACE_O_TRACECLONE|PTRACE_O_TRACEEXEC|PTRACE_O_EXITKILL;
    if(ptrace(PTRACE_SETOPTIONS,child,0,options) || ptrace(PTRACE_SYSCALL,child,0,0))die("start tracing");
    int exit_code=2;
    for(;;) {
        arm_notification();
        if(held_tid && negative_seen && !release_at)release_at=clock_ns()+100000000ULL;
        if(held_tid && ((release_at && clock_ns()>=release_at) || clock_ns()-held_at>10000000000ULL)) {
            if(ptrace(PTRACE_SYSCALL,held_tid,0,0))die("release held backend owner");
            printf("RELEASE_ORIGINAL_BACKEND_CQE after_negative=%d\n",negative_seen>0);fflush(stdout);
            held_tid=0;released=1;
        }
        pid_t tid=waitpid(-1,&status,__WALL|((held_tid||notify_mode)?WNOHANG:0));
        if(tid==0){usleep(1000);continue;}if(tid<0){if(errno==EINTR)continue;if(errno==ECHILD)break;die("wait");}
        if(WIFEXITED(status)||WIFSIGNALED(status)) {
            if(tid==child)exit_code=WIFEXITED(status)?WEXITSTATUS(status):128+WTERMSIG(status);
            struct thread *t=thread(tid);memset(t,0,sizeof(*t));continue;
        }
        if(!WIFSTOPPED(status))continue;
        int sig=WSTOPSIG(status),deliver=0;
        if(sig==(SIGTRAP|0x80)) {
            struct ptrace_syscall_info info;
            if(ptrace(PTRACE_GET_SYSCALL_INFO,tid,sizeof(info),&info)<0)die("syscall info");
            if(info.op==PTRACE_SYSCALL_INFO_ENTRY)enter(thread(tid),&info);
            else if(info.op==PTRACE_SYSCALL_INFO_EXIT) {
                int prior=held_once;
                leave(thread(tid),&info);
                if(!prior && held_once){held_tid=tid;held_owner=tid;held_at=clock_ns();continue;}
            }
        } else if(sig!=SIGTRAP && sig!=SIGSTOP)deliver=sig;
        if(ptrace(PTRACE_SYSCALL,tid,0,deliver) && errno!=ESRCH)die("continue");
    }
    printf("COMMAND_FAULT_SUMMARY injected=%d negative_seen=%d writes_before=%llu writes_after=%llu total_writes=%llu child_exit=%d\n",
           injected,negative_seen,(unsigned long long)writes_before,(unsigned long long)(writes-writes_before),(unsigned long long)writes,exit_code);
    if(hold_mode)printf("BACKEND_HOLD_SUMMARY held=%d released=%d drained=%d\n",held_once,released,drained);
    return exit_code;
}
