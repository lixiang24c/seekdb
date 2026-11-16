/* Test-only interposer. Never linked into the runtime image.
 * Pause at an armed boundary; the parent sends real SIGKILL (also for PID 1).
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>
#undef fwrite_unlocked

static int fired, writes;
static const char *root(void) { const char *p=getenv("MV_NATIVE_FAULT_ROOT"); return p?p:"/data"; }
static int armed(const char *point) {
    char path[4096],value[128];snprintf(path,sizeof(path),"%s/fault.arm",root());
    int fd=open(path,O_RDONLY);if(fd<0)return 0;
    ssize_t n=read(fd,value,sizeof(value)-1);close(fd);if(n<0)return 0;value[n]=0;
    return !strcmp(point,value);
}
static void pause_at(const char *point) {
    if(fired)return;fired=1;
    char path[4096];snprintf(path,sizeof(path),"%s/fault.reached",root());
    int fd=open(path,O_WRONLY|O_CREAT|O_TRUNC,0666);
    if(fd<0)_exit(98);syscall(SYS_write,fd,point,strlen(point));close(fd);
    for(;;)pause();
}
static int fd_path(int fd,char *path) {
    char link[64];snprintf(link,sizeof(link),"/proc/self/fd/%d",fd);
    ssize_t n=readlink(link,path,4095);if(n<0)return 0;path[n]=0;return 1;
}
static int named(int fd,const char *name) {
    char path[4096],wanted[4096];snprintf(wanted,sizeof(wanted),"%s/%s",root(),name);
    return fd_path(fd,path)&&!strcmp(path,wanted);
}
ssize_t write(int fd,const void *buf,size_t n) {
    static ssize_t (*real_write)(int,const void*,size_t);
    if(!real_write)real_write=dlsym(RTLD_NEXT,"write");
    if(!fired&&named(fd,"queryable.log")){
        char point[32];
        for(int i=1;i<=8;i++){snprintf(point,sizeof(point),"write-%d",i);
            if(armed(point)){if(++writes==i){size_t half=n/2;if(half)real_write(fd,buf,half);pause_at(point);}break;}}
    }
    if(!fired&&named(fd,"queryable.next")&&armed("checkpoint-write")){size_t half=n/2;if(half)real_write(fd,buf,half);pause_at("checkpoint-write");}
    return real_write(fd,buf,n);
}
int fsync(int fd) {
    static int (*real_sync)(int);if(!real_sync)real_sync=dlsym(RTLD_NEXT,"fsync");
    if(!fired&&named(fd,"queryable.log")){
        if(armed("sync-before"))pause_at("sync-before");
        if(armed("sync-eio")){fired=1;errno=EIO;return -1;}
        int result=real_sync(fd);if(!result&&armed("sync-after"))pause_at("sync-after");return result;
    }
    int next=named(fd,"queryable.next");
    if(!fired&&next&&armed("checkpoint-sync"))pause_at("checkpoint-sync");
    char path[4096];int directory=fd_path(fd,path)&&!strcmp(path,root());
    if(!fired&&directory&&armed("checkpoint-directory"))pause_at("checkpoint-directory");
    int result=real_sync(fd);
    if(!fired&&!result&&next&&armed("checkpoint-sync-after"))pause_at("checkpoint-sync-after");
    if(!fired&&!result&&directory&&armed("checkpoint-directory-after"))pause_at("checkpoint-directory-after");
    return result;
}
int rename(const char *old,const char *new) {
    static int (*real_rename)(const char*,const char*);if(!real_rename)real_rename=dlsym(RTLD_NEXT,"rename");
    char wanted[4096];snprintf(wanted,sizeof(wanted),"%s/queryable.next",root());
    int checkpoint=!strcmp(old,wanted);
    if(!fired&&checkpoint&&armed("checkpoint-before-rename"))pause_at("checkpoint-before-rename");
    int result=real_rename(old,new);
    if(!fired&&checkpoint&&!result&&armed("checkpoint-after-rename"))pause_at("checkpoint-after-rename");return result;
}
size_t fwrite_unlocked(const void *ptr,size_t size,size_t count,FILE *stream) {
    static size_t (*real_write)(const void*,size_t,size_t,FILE*);
    if(!real_write)real_write=dlsym(RTLD_NEXT,"fwrite_unlocked");
    size_t result=real_write(ptr,size,count,stream);char path[4096];
    if(!fired&&fd_path(fileno(stream),path)&&strstr(path,"/mv-")&&armed("replay-write"))pause_at("replay-write");
    return result;
}

/* Test-only read boundary for repeated interruption of catalog reconstruction. */
ssize_t pread(int fd,void *buf,size_t n,off_t offset) {
    static ssize_t (*real_read)(int,void*,size_t,off_t);
    if(!real_read)real_read=dlsym(RTLD_NEXT,"pread");
    ssize_t result=real_read(fd,buf,n,offset);
    if(!fired&&offset>24&&named(fd,"queryable.log")&&armed("recover-read"))pause_at("recover-read");
    return result;
}
