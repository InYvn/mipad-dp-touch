/* Unmapped candidate. Synchronous, bounded, ordered kernel acceptance only. */
#define _GNU_SOURCE
#include <unistd.h>
#include <sys/syscall.h>
#include <sys/stat.h>
#include <stdio.h>
#include <fcntl.h>
#include <stdarg.h>
#include <android/log.h>
#include "fullsplit-bounded.c"
#include "fullsplit-gate.h"
/* Production native-touch payload (module 2.1.10); lab build is separate. */
#ifdef FULLSPLIT_NATIVE
#include "fullsplit-native.h"
#endif
#ifndef FULLSPLIT_EXPLICIT
#define FULLSPLIT_EXPLICIT 1
#endif
#ifndef RX_TARGET_PATH
#define RX_TARGET_PATH "/dev/hidg1"
#endif
static RX state={.explicit_release=FULLSPLIT_EXPLICIT};
static _Thread_local int entered;
static int owner_fd=-1;
static dev_t device,rdevice;static ino_t inode;
static unsigned long sequence,input_sequence;
static int target_fd(int fd,struct stat*s){struct stat t;char p[64],name[128];if(fstat(fd,s)||!S_ISCHR(s->st_mode)||stat(RX_TARGET_PATH,&t))return 0;if(s->st_dev!=t.st_dev||s->st_ino!=t.st_ino||s->st_rdev!=t.st_rdev)return 0;snprintf(p,sizeof(p),"/proc/self/fd/%d",fd);ssize_t n=readlink(p,name,sizeof(name)-1);if(n<0)return 0;name[n]=0;return !strcmp(name,RX_TARGET_PATH);}
static void reset(const char*why,int fd){int e=errno;__android_log_print(ANDROID_LOG_INFO,"FullSplitBounded","reset=%s fd=%d generation=%llu pending=%d next=%d/%d seq=%lu",why,fd,(unsigned long long)state.generation,state.pending,state.next,state.total,sequence);rx_begin_session(&state,state.generation+1);errno=e;}
#ifdef FS_TEST_WRITER
extern ssize_t FS_TEST_WRITER(int,const void*,size_t);
#endif
/* Experimental lab build only (-DFULLSPLIT_LAB): probe/variant/no-grab hooks.
 * The production DSO build does not compile them in. */
#ifdef FULLSPLIT_LAB
#include "fullsplit-lab.h"
#define FS_WRITE_SYSCALL(fd,b,n) LAB_SYS_WRITE(fd,b,n)
#else
#ifdef FS_TEST_WRITER
#define FS_WRITE_SYSCALL(fd,b,n) FS_TEST_WRITER(fd,b,n)
#else
#define FS_WRITE_SYSCALL(fd,b,n) syscall(SYS_write,fd,b,n)
#endif
#endif
static ssize_t raw_write(int fd,const void*b,size_t n){
#ifdef FULLSPLIT_LAB
 /* Gate frames keep the factory layout internally; only the bytes handed to the
  * kernel are reordered, and only after the matching descriptor landed. */
 unsigned char mapped[62];const void*wb=b;size_t wn=n;
 if(lab_frame_map(b,n,mapped)){wb=mapped;wn=62;}
 ssize_t r=FS_WRITE_SYSCALL(fd,wb,wn);
#else
 ssize_t r=FS_WRITE_SYSCALL(fd,b,n);
#endif
 int e=errno;
#ifdef FULLSPLIT_LAB
 if(r==62&&n==62&&((const unsigned char*)b)[0]==1)lab_pen_probe_maybe(fd);
#endif
 if(n==62&&((const unsigned char*)b)[0]==1){char hex[125];for(int i=0;i<62;i++)snprintf(hex+i*2,3,"%02x",((const unsigned char*)b)[i]);__android_log_print(ANDROID_LOG_INFO,"FullSplitBounded","seq=%lu fd=%d generation=%llu rc=%zd errno=%d phase=%d/%d pending=%d hex=%s",++sequence,fd,(unsigned long long)state.generation,r,r<0?e:0,state.next,state.total,state.pending,hex);}errno=e;return r;
}
/* Android bionic has no pthread cancellation API. */
static int enter(int*cancel){*cancel=0;if(fs_enter())return -1;entered=1;return 0;}
static void leave(int cancel,int e){(void)cancel;entered=0;fs_leave();errno=e;}
ssize_t write(int fd,const void*b,size_t n){struct stat s;int saved=errno;
#ifdef FULLSPLIT_NATIVE
 {ssize_t nr;if(native_desc_try_write(fd,b,n,&nr))return nr;}
#endif
#ifdef FULLSPLIT_LAB
 lab_ensure();
 if(lab_enabled){ssize_t lr;if(lab_desc_try_write(fd,b,n,&lr))return lr;}
#endif
 if(entered){if(target_fd(fd,&s)){errno=EDEADLK;return -1;}return syscall(SYS_write,fd,b,n);}
 if(!target_fd(fd,&s)){errno=saved;return syscall(SYS_write,fd,b,n);}
#ifdef FULLSPLIT_LAB
 if(lab_enabled)lab_note_target_write(fd,b,n);
#endif
 int cancel;if(enter(&cancel))return -1;
 if(!target_fd(fd,&s)){leave(cancel,EBADF);return -1;}
 int flags=fcntl(fd,F_GETFL);if(flags<0||!(flags&O_NONBLOCK)){leave(cancel,flags<0?errno:EOPNOTSUPP);return -1;}
 if(owner_fd!=fd||device!=s.st_dev||inode!=s.st_ino||rdevice!=s.st_rdev){reset("target-generation",fd);owner_fd=fd;device=s.st_dev;inode=s.st_ino;rdevice=s.st_rdev;}
 ++input_sequence;
 if(n==62){char hex[125];for(int i=0;i<62;i++)snprintf(hex+i*2,3,"%02x",((const unsigned char*)b)[i]);__android_log_print(ANDROID_LOG_INFO,"FullSplitBounded","input_seq=%lu fd=%d generation=%llu prior_active=%d pending=%d next=%d/%d hex=%s",input_sequence,fd,(unsigned long long)state.generation,state.active,state.pending,state.next,state.total,hex);}
 errno=saved;ssize_t r=rx_send(&state,1,fd,b,n,raw_write);int e=errno;
 if(r<0)__android_log_print(ANDROID_LOG_INFO,"FullSplitBounded","failure fd=%d generation=%llu seq=%lu pending=%d next=%d/%d poison=%d errno=%d",fd,(unsigned long long)state.generation,sequence,state.pending,state.next,state.total,state.poison,e);
 leave(cancel,e);return r;
}
ssize_t __write_chk(int fd,const void*b,size_t n,size_t size){if(n>size)abort();return write(fd,b,n);}
int close(int fd){struct stat s;
#ifdef FULLSPLIT_LAB
 lab_ensure();
 if(lab_enabled)lab_close_note(fd);
#endif
 if(!target_fd(fd,&s))return syscall(SYS_close,fd);if(entered){errno=EDEADLK;return -1;}int cancel;if(enter(&cancel))return -1;int r=syscall(SYS_close,fd),e=errno;if(!r){reset("target-close",fd);owner_fd=-1;}leave(cancel,e);return r;}
int open(const char*p,int flags,...){mode_t mode=0;if(flags&O_CREAT||(flags&O_TMPFILE)==O_TMPFILE){va_list a;va_start(a,flags);mode=va_arg(a,int);va_end(a);}
#ifdef FULLSPLIT_LAB
 lab_ensure();
 if(lab_enabled&&strcmp(p,RX_TARGET_PATH)){int lr=syscall(SYS_openat,AT_FDCWD,p,flags,mode);int le=errno;lab_open_note(p,flags,-1,lr,lr<0?le:0);errno=le;return lr;}
#endif
 if(strcmp(p,RX_TARGET_PATH))return syscall(SYS_openat,AT_FDCWD,p,flags,mode);if(entered){errno=EDEADLK;return -1;}int cancel;if(enter(&cancel))return -1;int r=syscall(SYS_openat,AT_FDCWD,p,flags,mode),e=errno;if(r>=0){reset("target-open",r);owner_fd=-1;}
#ifdef FULLSPLIT_LAB
 if(lab_enabled)lab_open_note(p,flags,-1,r,r<0?e:0);
#endif
 leave(cancel,e);return r;}
int open64(const char*p,int flags,...){mode_t mode=0;if(flags&O_CREAT||(flags&O_TMPFILE)==O_TMPFILE){va_list a;va_start(a,flags);mode=va_arg(a,int);va_end(a);}return open(p,flags,mode);}
int __open_2(const char*p,int flags){if(flags&O_CREAT||(flags&O_TMPFILE)==O_TMPFILE)abort();return open(p,flags);}
static int duplicate(int a,int b,int flags,int is2){struct stat s,t;int affected=target_fd(a,&s)||target_fd(b,&t);if(entered&&affected){errno=EDEADLK;return -1;}int cancel=0;if(affected&&enter(&cancel))return -1;int r=(is2&&a==b)?(fcntl(a,F_GETFD)>=0?a:-1):syscall(SYS_dup3,a,b,flags);int e=errno;
#ifdef FULLSPLIT_LAB
 if(lab_enabled&&r>=0&&a!=b)lab_dup_note(a,b,r);
#endif
 if(affected){if(r>=0&&a!=b){reset("target-dup",b);owner_fd=-1;}leave(cancel,e);}errno=e;return r;}
int dup2(int a,int b){return duplicate(a,b,0,1);}
int dup3(int a,int b,int flags){return duplicate(a,b,flags,0);}
