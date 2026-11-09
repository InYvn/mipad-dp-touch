#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <signal.h>
#include <errno.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <poll.h>
#include <time.h>
static volatile sig_atomic_t stopping;
static void stop(int s){(void)s;stopping=1;}
static double now(void){struct timespec t;clock_gettime(CLOCK_MONOTONIC,&t);return t.tv_sec+t.tv_nsec/1e9;}
static int exists(const char*m,const char*n){char b[4096];snprintf(b,sizeof b,"%s/%s",m,n);return access(b,F_OK)==0;}
static void closefds(void){long n=sysconf(_SC_OPEN_MAX);if(n<0||n>65536)n=65536;for(int i=3;i<n;i++)close(i);}
/* Adapter is an explicit capability: production pins its path in service.sh.
 * Every operation is bounded; a timed out mutation leaves a durable journal.
 * Recovery never depends on the worker's inherited pipes or descriptors. */
static int action(const char*a,const char*op,const char*s,const char*m){
 int recovering=strcmp(op,"reconcile")!=0;
 pid_t p=fork();if(p<0)return 90;if(!p){if(recovering)setpgid(0,0);closefds();const char*bb=getenv("MIPAD_PLATFORM_BUSYBOX");if(bb&&*bb)execl(bb,bb,"sh","-o","standalone",a,op,s,m,NULL);else execl(a,a,op,s,m,NULL);_exit(127);}if(recovering)setpgid(p,p);
 double end=now()+35;int st=0;
 while(waitpid(p,&st,WNOHANG)==0){if(now()>end){kill(recovering?-p:-getpid(),SIGKILL);waitpid(p,&st,0);return 124;}usleep(50000);}
 int rc=WIFEXITED(st)?WEXITSTATUS(st):128+WTERMSIG(st);
 if(rc)fprintf(stderr,"CONTROLLER_ACTION wall=%lld mono=%.6f guardian=%d child=%d op=%s rc=%d status=%d\n",(long long)time(NULL),now(),getpid(),p,op,rc,st);
 return rc;
}
int main(int argc,char**argv){
 if(argc!=5||(strcmp(argv[1],"run")&&strcmp(argv[1],"once")&&strcmp(argv[1],"monitor")))return 64;
 const char*m=argv[2],*s=argv[3],*a=argv[4];char b[4096];
 struct stat st;if(lstat(s,&st)||!S_ISDIR(st.st_mode)||st.st_uid!=getuid()||(st.st_mode&0022))return 65;
 if(!strcmp(argv[1],"monitor")){
  /* Independent lifetime lock: never unlink or replace either lock inode.
   * The shell and its active controller retain the reference across exec;
   * platform actions/prepare clients close inherited FDs as before. */
  snprintf(b,sizeof b,"%s/monitor.lock",s);
  int ml=open(b,O_CREAT|O_RDWR|O_NOFOLLOW,0600);
  if(ml<0)return 73;
  if(fstat(ml,&st)||!S_ISREG(st.st_mode)||st.st_uid!=getuid()||(st.st_mode&0077)){close(ml);return 65;}
  if(flock(ml,LOCK_EX|LOCK_NB)){int e=errno;close(ml);return e==EWOULDBLOCK?0:73;}
  const char*bb=getenv("MIPAD_PLATFORM_BUSYBOX");
  if(bb&&*bb)execl(bb,bb,"sh","-o","standalone",a,m,s,"--monitor-locked",NULL);
  else execl("/bin/sh","sh",a,m,s,"--monitor-locked",NULL);
  return 127;
 }
 snprintf(b,sizeof b,"%s/controller.lock",s);int lock=open(b,O_CREAT|O_RDWR|O_NOFOLLOW|O_CLOEXEC,0600);
 if(lock<0||flock(lock,LOCK_EX|LOCK_NB))return 73;
 signal(SIGTERM,stop);signal(SIGINT,stop);signal(SIGHUP,stop);signal(SIGPIPE,SIG_IGN);
 if(!strcmp(argv[1],"once"))return action(a,"stage",s,m);
 /* Recover a previous boot/worker generation before attempting a new one. */
 int rc=action(a,"recover",s,m);if(rc)return rc;
 if(exists(m,"disable")||exists(m,"remove")||access(m,F_OK))return 0;
 int hb[2];if(pipe(hb))return 74;pid_t worker=fork();if(worker<0)return 75;
 if(!worker){setpgid(0,0);close(hb[0]);signal(SIGTERM,stop);
  for(;;){if(stopping||exists(m,"disable")||exists(m,"remove")||access(m,F_OK))_exit(0);
   char c='R';if(write(hb[1],&c,1)!=1)_exit(76);
   int r=action(a,"reconcile",s,m);if(r&&r!=75)_exit(r);
   for(int i=0;i<10&&!stopping;i++)usleep(100000);
  }
 }
 close(hb[1]);snprintf(b,sizeof b,"%s/worker.pid",s);FILE*f=fopen(b,"w");if(!f){kill(worker,SIGTERM);return 77;}fprintf(f,"%d\n",worker);fclose(f);
 /* The lock-owning guardian is armed before reconcile can mutate anything. */
 double deadline=now()+45;int status=0;int dead=0;
 while(!dead){struct pollfd q={hb[0],POLLIN|POLLHUP,0};int r=poll(&q,1,200);if(r>0&&(q.revents&POLLIN)){char tmp[64];if(read(hb[0],tmp,sizeof tmp)>0)deadline=now()+45;}
  if(stopping||now()>deadline){kill(worker,SIGTERM);double e=now()+2;while(now()<e){if(waitpid(worker,&status,WNOHANG)==worker){dead=1;break;}usleep(50000);}if(!dead){kill(worker,SIGKILL);waitpid(worker,&status,0);dead=1;}}
  if(!dead&&waitpid(worker,&status,WNOHANG)==worker)dead=1;
 }
 kill(-worker,SIGKILL);close(hb[0]);unlink(b);
 rc=action(a,"recover",s,m);if(rc)return rc;
 return WIFEXITED(status)?WEXITSTATUS(status):128+WTERMSIG(status);
}
