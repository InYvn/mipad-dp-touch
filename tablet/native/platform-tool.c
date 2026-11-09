#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <signal.h>
#include <dirent.h>
#include <errno.h>
#include <grp.h>
#include <stdint.h>
#include <sys/stat.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <poll.h>
#ifndef FS_HAL
#define FS_HAL "/odm/bin/vendor.xiaomi.usb.dp-service"
#endif
#ifndef FS_CONTEXT
#define FS_CONTEXT "u:r:hal_usb_dp_relay_default:s0"
#endif
#ifndef FS_UID_STATUS
#define FS_UID_STATUS "Uid:\t0\t0\t0\t0\n"
#define FS_GID_STATUS "Gid:\t1000\t1000\t1000\t1000\n"
#define FS_GROUPS_STATUS "Groups:\t1004 1018 3011 \n"
#endif
#ifndef FS_HID0
#define FS_HID0 "/dev/hidg0"
#define FS_HID1 "/dev/hidg1"
#endif
#define HAL FS_HAL
#define CTX FS_CONTEXT
struct ident {pid_t pid;unsigned long long start;char exe[1024];};
static int text(const char*p,char*b,size_t n){int f=open(p,O_RDONLY|O_CLOEXEC);if(f<0)return -1;size_t k=0;ssize_t r;while(k<n-1&&(r=read(f,b+k,n-1-k))>0)k+=(size_t)r;close(f);b[k]=0;return (int)k;}
static unsigned long long start(pid_t p){char path[96],b[4096];snprintf(path,sizeof path,"/proc/%d/stat",p);if(text(path,b,sizeof b)<0)return 0;char*s=strrchr(b,')');if(!s)return 0;s+=2;char*save;int k=0;for(char*t=strtok_r(s," ",&save);t;t=strtok_r(NULL," ",&save),k++)if(k==19)return strtoull(t,0,10);return 0;}
static int linkeq(const char*p,const char*v){char b[2048];ssize_t n=readlink(p,b,sizeof b-1);if(n<0)return 0;b[n]=0;return !strcmp(b,v);}
static int exeis(pid_t p,const char*v){char path[96];snprintf(path,sizeof path,"/proc/%d/exe",p);return linkeq(path,v);}
static int same(struct ident id){return id.pid>1&&id.start&&start(id.pid)==id.start&&exeis(id.pid,id.exe);}
static int getid(const char*file,struct ident*id){FILE*f=fopen(file,"r");if(!f)return 0;char b[2200],extra;int used=0;int ok=fgets(b,sizeof b,f)!=NULL;int trailing=fgetc(f);fclose(f);if(!ok||trailing!=EOF||!strchr(b,'\n'))return 0;int r=sscanf(b,"%d %llu %n",&id->pid,&id->start,&used);if(r!=2||!used||id->pid<=1||!id->start)return 0;char*s=b+used;size_t n=strcspn(s,"\n");if(n==0||n>=sizeof id->exe||s[n+1]!=0||memchr(s,'\r',n))return 0;memcpy(id->exe,s,n);id->exe[n]=0;(void)extra;return 1;}
static int save(const char*file,struct ident id){char tmp[2048];snprintf(tmp,sizeof tmp,"%s.new",file);int fd=open(tmp,O_CREAT|O_EXCL|O_WRONLY|O_NOFOLLOW,0600);if(fd<0)return 0;char b[2200];int n=snprintf(b,sizeof b,"%d %llu %s\n",id.pid,id.start,id.exe);int ok=write(fd,b,n)==n&&fsync(fd)==0;close(fd);if(!ok||rename(tmp,file))return 0;char dir[2048];snprintf(dir,sizeof dir,"%s",file);char *s=strrchr(dir,'/');if(s){*s=0;fd=open(dir,O_DIRECTORY|O_RDONLY);if(fd<0)return 0;ok=fsync(fd)==0;close(fd);}return ok;}
static int launch_owned(struct ident id){
 if(same(id))return 1;
 if(id.pid<=1||!id.start||start(id.pid)!=id.start)return 0;
 char self[2048];ssize_t n=readlink("/proc/self/exe",self,sizeof self-1);if(n<0)return 0;self[n]=0;
 /* A journaled fork may still be this helper before exec. The exact birth
  * generation is authoritative; never let this child escape rollback. */
 return exeis(id.pid,self);
}
static int terminate(struct ident id){int fd=(int)syscall(SYS_pidfd_open,id.pid,0);if(fd<0)return !launch_owned(id);if(!launch_owned(id)){close(fd);return 1;}syscall(SYS_pidfd_send_signal,fd,SIGTERM,NULL,0);for(int i=0;i<20&&launch_owned(id);i++)usleep(50000);if(launch_owned(id))syscall(SYS_pidfd_send_signal,fd,SIGKILL,NULL,0);close(fd);for(int i=0;i<20&&launch_owned(id);i++)usleep(50000);return !launch_owned(id);}
static pid_t find(const char*exe){DIR*d=opendir("/proc");if(!d)return -1;pid_t found=0;struct dirent*e;while((e=readdir(d))){pid_t p=atoi(e->d_name);if(p>1&&exeis(p,exe)){if(found){closedir(d);return -1;}found=p;}}closedir(d);return found;}
static int identity(pid_t p,int original){char path[96],b[8192];if(!exeis(p,HAL))return 0;snprintf(path,sizeof path,"/proc/%d/attr/current",p);if(text(path,b,sizeof b)<0||strcmp(b,CTX))return 0;snprintf(path,sizeof path,"/proc/%d/status",p);if(text(path,b,sizeof b)<0||!strstr(b,FS_UID_STATUS)||!strstr(b,FS_GID_STATUS)||!strstr(b,FS_GROUPS_STATUS)||(original&&!strstr(b,"PPid:\t1\n")))return 0;return 1;}
static int securezero(pid_t p){char path[96];snprintf(path,sizeof path,"/proc/%d/auxv",p);int f=open(path,O_RDONLY);if(f<0)return 0;uint64_t v[2];int ok=0;while(read(f,v,sizeof v)==sizeof v){if(v[0]==23){ok=v[1]==0;break;}}close(f);return ok;}
static int maps(pid_t p,const char*dso,int loaded){char path[96],b[4096];snprintf(path,sizeof path,"/proc/%d/maps",p);FILE*f=fopen(path,"r");if(!f)return 0;int hit=0,bad=0;while(fgets(b,sizeof b,f)){char*name=strchr(b,'/');if(name){name[strcspn(name,"\r\n")]=0;hit|=!strcmp(name,dso)&&strstr(b," r-xp ")!=NULL;}if(!loaded&&(strstr(b,"fullsplit")||strstr(b,"deploy-interposer")||strstr(b,"loader-marker")||strstr(b,"releasexy")))bad=1;}fclose(f);return loaded?hit:!bad;}
static int owner(pid_t h,pid_t required){char dir[96],path[512],b[4096];snprintf(dir,sizeof dir,"/proc/%d/fd",h);DIR*d=opendir(dir);if(!d)return 0;int h0=0,h1=0;pid_t found=0;struct dirent*e;while((e=readdir(d))){if(e->d_name[0]=='.')continue;snprintf(path,sizeof path,"%s/%s",dir,e->d_name);h0|=linkeq(path,FS_HID0);h1|=linkeq(path,FS_HID1);if(linkeq(path,"anon_inode:[pidfd]")){snprintf(path,sizeof path,"/proc/%d/fdinfo/%s",h,e->d_name);if(text(path,b,sizeof b)>=0){char*s=strstr(b,"Pid:\t");pid_t p=s?atoi(s+5):0;if(p>1&&start(p)){if(found&&found!=p){closedir(d);return 0;}found=p;}}}}closedir(d);return h0&&h1&&found>1&&(!required||required==found)?found:0;}
static int launch(const char*kind,const char*exe,const char*preload,const char*lib,const char*record){
 int pipefd[2];if(pipe2(pipefd,O_CLOEXEC))return 10;pid_t p=fork();if(p<0)return 11;
 if(!p){close(pipefd[1]);char c;if(read(pipefd[0],&c,1)!=1||c!='G')_exit(80);close(pipefd[0]);setsid();int f=open("/dev/null",O_RDWR);if(f<0)_exit(81);for(int i=0;i<3;i++)if(dup2(f,i)<0)_exit(82);if(f>2)close(f);for(int i=3;i<65536;i++)close(i);
  unsetenv("LD_PRELOAD");unsetenv("LD_LIBRARY_PATH");
  if(!strcmp(kind,"hal")){gid_t g[]={1004,1018,3011};if(setgroups(3,g)||setresgid(1000,1000,1000)||setresuid(0,0,0))_exit(83);f=open("/proc/self/attr/exec",O_WRONLY);if(f<0||write(f,CTX,strlen(CTX))!=(ssize_t)strlen(CTX))_exit(84);close(f);if(setenv("LD_PRELOAD",preload,1)||setenv("LD_LIBRARY_PATH",lib,1))_exit(85);}
#ifdef FS_TEST_PREEXEC_DELAY
  sleep(FS_TEST_PREEXEC_DELAY);
#endif
  if(!strcmp(kind,"owner-rebuild"))execl(exe,exe,"--rebuild",NULL);
  else execl(exe,exe,NULL);
  _exit(86);
 }
 close(pipefd[0]);struct ident id={p,start(p),""};snprintf(id.exe,sizeof id.exe,"%s",exe);
 if(!id.start||!save(record,id)){close(pipefd[1]);waitpid(p,NULL,0);return 12;}
 int ok=write(pipefd[1],"G",1)==1;close(pipefd[1]);return ok?0:13;
}
int main(int argc,char**argv){
 if(argc==3&&!strcmp(argv[1],"find")){pid_t p=find(argv[2]);if(p<1)return p<0?3:2;printf("%d %llu %s\n",p,start(p),argv[2]);return 0;}
 if(argc==3&&!strcmp(argv[1],"gone")){struct ident id;if(!getid(argv[2],&id))return 1;int fd=(int)syscall(SYS_pidfd_open,id.pid,0);if(fd<0)return errno==ESRCH?0:1;struct pollfd f={fd,POLLIN,0};int exited=poll(&f,1,0)>0&&(f.revents&POLLIN);unsigned long long current=start(id.pid);close(fd);return exited||(current&&current!=id.start)?0:1;}
 if(argc==3&&!strcmp(argv[1],"same")){struct ident id;return getid(argv[2],&id)&&same(id)?0:1;}
 if(argc==3&&!strcmp(argv[1],"terminate")){struct ident id;return getid(argv[2],&id)&&terminate(id)?0:1;}
 if(argc==3&&!strcmp(argv[1],"original")){struct ident id;return getid(argv[2],&id)&&same(id)&&identity(id.pid,1)&&maps(id.pid,"fullsplit",0)?0:1;}
 if(argc==3&&!strcmp(argv[1],"baseline")){struct ident id;if(!getid(argv[2],&id)||!same(id)||!identity(id.pid,1)||!maps(id.pid,"fullsplit",0))return 1;int o=owner(id.pid,0);if(!o)return 2;printf("%d %llu\n",o,start(o));return 0;}
 if(argc==6&&!strcmp(argv[1],"verify")){struct ident h,o;int loaded=strcmp(argv[4],"original")!=0;if(!getid(argv[2],&h)||!getid(argv[3],&o)||!same(h)||!same(o)||!identity(h.pid,!loaded)||!owner(h.pid,o.pid)||!maps(h.pid,argv[5],loaded)||(loaded&&!securezero(h.pid)))return 1;return 0;}
 if(argc==7&&!strcmp(argv[1],"launch"))return launch(argv[2],argv[3],argv[4],argv[5],argv[6]);
 return 64;
}
