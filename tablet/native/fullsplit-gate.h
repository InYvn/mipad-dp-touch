/* FIFO reservation. Mutex is held ONLY for queue operations, never I/O/wait.
 * A timed-out waiter is unlinked before its stack record expires. */
#include <pthread.h>
#include <errno.h>
#include <time.h>
typedef struct fs_waiter {struct fs_waiter*next;} fs_waiter;
static pthread_mutex_t fs_mutex=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t fs_cond=PTHREAD_COND_INITIALIZER;
static fs_waiter *fs_head,*fs_tail;
static int fs_busy,fs_waiting;
#define FS_MAX_WAITERS 16
static int fs_enter(void){
 struct timespec until;
#ifdef __ANDROID__
 clock_gettime(CLOCK_MONOTONIC,&until);
#else
 clock_gettime(CLOCK_REALTIME,&until);
#endif
 until.tv_nsec+=100000000;if(until.tv_nsec>=1000000000){until.tv_sec++;until.tv_nsec-=1000000000;}
 fs_waiter w={0};pthread_mutex_lock(&fs_mutex);
 if(fs_waiting>=FS_MAX_WAITERS){pthread_mutex_unlock(&fs_mutex);errno=EBUSY;return -1;}fs_waiting++;
 if(fs_tail)fs_tail->next=&w;else fs_head=&w;fs_tail=&w;
 int e=0;while(fs_busy||fs_head!=&w){
#ifdef __ANDROID__
 e=pthread_cond_clockwait(&fs_cond,&fs_mutex,CLOCK_MONOTONIC,&until);
#else
 e=pthread_cond_timedwait(&fs_cond,&fs_mutex,&until);
#endif
 if(e)break;}
 if(e){fs_waiter**p=&fs_head;while(*p&&*p!=&w)p=&(*p)->next;if(*p)*p=w.next;if(fs_tail==&w){fs_tail=fs_head;while(fs_tail&&fs_tail->next)fs_tail=fs_tail->next;}pthread_cond_broadcast(&fs_cond);}
 else {fs_head=w.next;if(!fs_head)fs_tail=NULL;fs_busy=1;}
 fs_waiting--;pthread_mutex_unlock(&fs_mutex);if(e){errno=e;return -1;}return 0;
}
static void fs_leave(void){pthread_mutex_lock(&fs_mutex);fs_busy=0;pthread_cond_broadcast(&fs_cond);pthread_mutex_unlock(&fs_mutex);}
