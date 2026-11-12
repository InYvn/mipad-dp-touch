/* Offline/controlled-trial candidate: packet writes, never suffix writes. */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <errno.h>
typedef ssize_t (*writer)(int,const void*,size_t);
typedef struct {uint64_t generation;int explicit_release,active,poison,pending,fd,next,total,retries;unsigned char prior[62],input[62],out[3][62];} RX;
static void rx_reset(RX*s){int m=s->explicit_release;uint64_t g=s->generation;memset(s,0,sizeof(*s));s->explicit_release=m;s->generation=g;}
static void rx_begin_session(RX*s,uint64_t g){rx_reset(s);s->generation=g;}
static int parse(const unsigned char*b,int*single){unsigned seen=0;int active=0;if(b[61]>10)return -1;for(int i=0;i<b[61];i++){int o=1+6*i,id=b[o+1];if(id>9||(seen&(1u<<id)))return -1;seen|=1u<<id;if(b[o]&3){active++;*single=i;}}return active;}
/* A report-ID-1 frame with no contacts down. The host has no contact state to
 * contradict, so writing it whole is always safe: it is the only way back from a
 * poisoned gate, where the host-side state is unknown. */
static int rx_release_all(const unsigned char*b,size_t n){return n==62&&b[0]==1&&b[61]==0;}
static ssize_t rx_send(RX*s,int target,int fd,const void*buf,size_t n,writer fn){
 const unsigned char*b=buf;if(!target)return fn(fd,buf,n);
 if(s->poison&&!((n==62&&b[0]==3)||rx_release_all(b,n))){errno=EIO;return -1;}
 if(s->poison){
  if(b[0]==3)return fn(fd,buf,n);/* pen frames keep their pass-through semantics */
  /* Release-all while poisoned: try the whole 62B packet once. A short/failed write
   * leaves the poison in place (fail-closed); a full write proves the host has no
   * contacts, which is exactly the state a fresh session assumes. */
  ssize_t rc=fn(fd,buf,n);int e=errno;if(rc!=62){errno=e;return rc;}
  rx_reset(s);memcpy(s->prior,b,62);s->active=0;return 62;
 }
 if(!n||b[0]!=1)return fn(fd,buf,n);
 int single=0,a=n==62?parse(b,&single):-1;
 if(s->pending){if(fd!=s->fd||n!=62||memcmp(b,s->input,62)){s->poison=1;errno=EIO;return -1;}}
 else {
  if(a<0){rx_reset(s);return fn(fd,buf,n);}
  memcpy(s->input,b,62);s->fd=fd;s->next=0;s->total=1;s->retries=0;memcpy(s->out[0],b,62);
  if(s->active>=2&&a==1){int j=0;if(s->explicit_release){unsigned char*r=s->out[j++];memset(r,0,62);r[0]=1;for(int i=0;i<s->prior[61];i++){int o=1+6*i;if(s->prior[o]&3){int dest=1+6*r[61]++;memcpy(r+dest,s->prior+o,6);r[dest]=0;}}}
   memset(s->out[j],0,62);s->out[j++][0]=1;
   memset(s->out[j],0,62);s->out[j][0]=1;s->out[j][61]=1;memcpy(s->out[j]+1,b+1+6*single,6);s->total=j+1;
  }s->pending=1;
 }
 while(s->next<s->total){ssize_t rc=fn(fd,s->out[s->next],62);int e=errno;
  if(rc!=62){if(rc<0&&(e==EINTR||e==EAGAIN)&&++s->retries<8){errno=e;return rc;}
   /* Nothing has been written for this transaction yet (next==0): the host is still
    * in the state prior/active describes, so drop the attempt and let the next frame
    * start over instead of poisoning. */
   if(rc<0&&s->next==0){s->pending=0;s->retries=0;errno=e;return rc;}
   s->poison=1;s->pending=0;/* emitted prefix cannot be rolled back */errno=e;return rc;}
  s->next++;
 }
 s->pending=0;s->active=a;memcpy(s->prior,b,62);return 62;
}
