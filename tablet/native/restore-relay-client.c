#include <dlfcn.h>
#include <android/binder_ibinder.h>
#include <android/binder_parcel.h>
#include <android/binder_status.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>
static void *create(void *p){return p;}
static void destroy(void*p){(void)p;}
static binder_status_t tx(AIBinder*b,transaction_code_t c,const AParcel*i,AParcel*o){(void)b;(void)c;(void)i;(void)o;return STATUS_UNKNOWN_TRANSACTION;}
static int call(AIBinder*b,transaction_code_t code){
 AParcel*in=NULL,*out=NULL; binder_status_t rc=AIBinder_prepareTransaction(b,&in);
 if(rc)return 4;
 rc=AIBinder_transact(b,code,&in,&out,0);
 printf("LIFECYCLE_TRANSACTION code=%u rc=%d pid=%d\n",code,rc,getpid());fflush(stdout);
 if(rc)return 5;
 AStatus*s=NULL;rc=AParcel_readStatusHeader(out,&s);
 int ok=!rc&&s&&AStatus_isOk(s);
 printf("LIFECYCLE_STATUS code=%u read=%d ok=%d exception=%d\n",code,rc,ok,s?AStatus_getExceptionCode(s):-999);fflush(stdout);
 if(s)AStatus_delete(s);AParcel_delete(out);
 return ok?0:6;
}
int main(int argc,char**argv){
 int rebuild=argc==2&&!strcmp(argv[1],"--rebuild");
 int release=argc==2&&!strcmp(argv[1],"--release");
 if(argc!=1&&!rebuild&&!release)return 64;
 const char*n="vendor.xiaomi.usb.dp.IUsbDpRelay";
 AIBinder*(*check)(const char*)=dlsym(RTLD_DEFAULT,"AServiceManager_checkService");if(!check)return 7;
 AIBinder*b=check("vendor.xiaomi.usb.dp.IUsbDpRelay/default");if(!b)return 2;
 AIBinder_Class*c=AIBinder_Class_define(n,create,destroy,tx);if(!AIBinder_associateClass(b,c))return 3;
 int rc;
 if(release)return call(b,6);
 /* A fresh HAL can inherit an existing gadget but have no bridge-ready flag.
  * Prepare establishes lifecycle ownership before release; final prepare owns
  * rebuilt endpoints. Keep this SAME process alive for all pidfd generations. */
 if((rc=call(b,5)))return rc;
 if(rebuild){if((rc=call(b,6)))return rc;if((rc=call(b,5)))return rc;}
 for(;;)sleep(60);
}
