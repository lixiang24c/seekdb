/* Test-only process-crash injector: cut a second table's WAL append after the
   first table's WAL has completed fdatasync. Enabled by an external arm file. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>

static char first_wal[PATH_MAX];
static int armed_wal(int fd,char *path) {
    const char *marker=getenv("MVDB_CUT_ARM");
    if (!marker || access(marker,F_OK)!=0) return 0;
    char descriptor[64];snprintf(descriptor,sizeof descriptor,"/proc/self/fd/%d",fd);
    ssize_t n=readlink(descriptor,path,PATH_MAX-1);
    if (n<0) return 0;
    path[n]=0;size_t length=(size_t)n;
    return length>=10 && !strcmp(path+length-10,"/pages.wal");
}

int fdatasync(int fd) {
    int (*real_sync)(int)=dlsym(RTLD_NEXT,"fdatasync");
    int result=real_sync(fd);char path[PATH_MAX];
    if (result==0 && !first_wal[0] && armed_wal(fd,path)) strcpy(first_wal,path);
    return result;
}

ssize_t write(int fd,const void *buffer,size_t length) {
    ssize_t (*real_write)(int,const void*,size_t)=dlsym(RTLD_NEXT,"write");
    char path[PATH_MAX];
    if (first_wal[0] && armed_wal(fd,path) && strcmp(first_wal,path)) {
        kill(getpid(),SIGKILL);_exit(99);
    }
    return real_write(fd,buffer,length);
}
