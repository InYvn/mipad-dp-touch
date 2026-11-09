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
static int64_t fs_now_ms(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return (int64_t)t.tv_sec*1000+t.tv_nsec/1000000;}
static _Thread_local writer fs_writer;
static _Thread_local int64_t fs_deadline;
static ssize_t fs_retry_write(int fd,const void*b,size_t n){
 for(;;){if(fs_now_ms()>=fs_deadline){errno=ETIMEDOUT;return -1;}
  ssize_t r=fs_writer(fd,b,n);int e=errno;
  if(r>=0||!(e==EAGAIN||e==EWOULDBLOCK||e==EINTR))return r;
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
 if(!target)return fn(fd,b,n);
 if(s->poison){errno=EIO;return -1;}
 fs_writer=fn;fs_deadline=fs_now_ms()+FS_DEADLINE_MS;
 ssize_t r=rx_send_legacy(s,target,fd,b,n,fs_retry_write);int e=errno;
 /* A partial expanded transaction has no representable input byte count. */
 if(r<0||((n==62&&((const unsigned char*)b)[0]==1)&&r!=62)){
  s->poison=1;if(r>=0){r=-1;e=EIO;}
 }
 errno=e;return r;
}
