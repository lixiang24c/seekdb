/* Test-only syscall fault injection; never copied into the submission image. */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
int fsync(int fd) {
    int (*real_sync)(int) = dlsym(RTLD_NEXT, "fsync");
    const char *root=getenv("MV_TEST_ROOT"),*mode=getenv("MV_TEST_FAULT");
    if(root && mode){
        char link[64],path[4096],armed[4096];
        snprintf(link,sizeof(link),"/proc/self/fd/%d",fd);
        ssize_t n=readlink(link,path,sizeof(path)-1);
        snprintf(armed,sizeof(armed),"%s/armed",root);
        if(n>0){path[n]=0;
            if(!strncmp(path,root,strlen(root)) && strstr(path,".index") && !access(armed,F_OK)){
                if(!strcmp(mode,"kill") || !strcmp(mode,"eio")){
                    char reached[4096];snprintf(reached,sizeof(reached),"%s/fault.reached",root);
                    int marker=open(reached,O_WRONLY|O_CREAT|O_TRUNC,0600);
                    if(marker>=0){write(marker,mode,strlen(mode));close(marker);}
                }
                if(!strcmp(mode,"kill")){
                    const char *external=getenv("MV_TEST_EXTERNAL_KILL");
                    /* PID namespace init cannot reliably SIGKILL itself.
                       Wait at this exact syscall boundary for the host. */
                    if(external && !strcmp(external,"1"))for(;;)pause();
                    kill(getpid(),SIGKILL);_exit(99);
                }
                if(!strcmp(mode,"eio")){errno=EIO;return -1;}
            }
        }
    }
    return real_sync(fd);
}
