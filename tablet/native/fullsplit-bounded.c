/* Synchronous experimental adapter: success means all kernel writes returned 62,
 * NOT USB completion/host receipt. No async acceptance contract is invented. */
#include <poll.h>
#include <time.h>
#define rx_send rx_send_legacy
#include "fullsplit.c"
#undef rx_send
#ifndef FS_WAIT
#define FS_WAIT poll
#endif
#define FS_DEADLINE_MS 100
#define FS_RETRY_MS 8 /* conservative retry quantum, NOT measured USB bInterval */
#ifndef ESHUTDOWN
#define ESHUTDOWN 108 /* bionic: "Cannot send after transport endpoint shutdown" */
#endif
static int64_t fs_now_ms(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return (int64_t)t.tv_sec*1000+t.tv_nsec/1000000;}
static _Thread_local writer fs_writer;
static _Thread_local int64_t fs_deadline;
/* errno values that are known to carry no bytes and are worth retrying inside the
 * existing deadline. ESHUTDOWN/ENODEV are included: a gadget endpoint that is being
 * (re)enumerated is transient, and a failed whole-report write leaves no partial state
 * on the host. Everything else (e.g. EIO) is returned to the caller unchanged. */
static int fs_transient(int e){return e==EAGAIN||e==EWOULDBLOCK||e==EINTR||e==ESHUTDOWN||e==ENODEV;}
static ssize_t fs_retry_write(int fd,const void*b,size_t n){
 for(;;){if(fs_now_ms()>=fs_deadline){errno=ETIMEDOUT;return -1;}
  ssize_t r=fs_writer(fd,b,n);int e=errno;
  if(r>=0||!fs_transient(e))return r;
  int64_t start=fs_now_ms();int remaining=(int)(fs_deadline-start);
  if(remaining<=0){errno=ETIMEDOUT;return -1;}
  int quantum=remaining<FS_RETRY_MS?remaining:FS_RETRY_MS;
  struct pollfd p={.fd=fd,.events=POLLOUT};
  int rc=FS_WAIT(&p,1,quantum);
  if(rc<0&&errno!=EINTR)return -1;
  if(rc>0&&(p.revents&(POLLERR|POLLHUP|POLLNVAL))){errno=EIO;return -1;}
  /* POLLOUT can be stale/spurious. Bound CPU and retry rate as well as time. */
  int rest=quantum-(int)(fs_now_ms()-start);
  if(rest>0)FS_WAIT(NULL,0,rest);
 }
}
static ssize_t rx_send(RX*s,int target,int fd,const void*b,size_t n,writer fn){
 const unsigned char*p=b;
 if(!target)return fn(fd,b,n);
 /* Poisoned: let through only the frames that cannot claim an active contact - the pen
  * pass-through and the release-all report the core uses to recover. */
 if(s->poison&&!((n==62&&p[0]==3)||rx_release_all(p,n))){errno=EIO;return -1;}
 fs_writer=fn;fs_deadline=fs_now_ms()+FS_DEADLINE_MS;
 ssize_t r=rx_send_legacy(s,target,fd,b,n,fs_retry_write);int e=errno;
 if(n==62&&p[0]==1&&r!=62){
  /* Fail closed only when bytes may have reached the host. A failure with next==0 has
   * written nothing, so prior/active still describe the host and the next frame may
   * simply try again. */
  if(r>=0||s->next>0)s->poison=1;
  else if(!fs_transient(e))s->pending=0;
  /* A partial/short expanded transaction has no representable input byte count. */
  if(r>=0){r=-1;e=EIO;}
 }
 errno=e;return r;
}
