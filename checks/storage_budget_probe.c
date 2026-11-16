/* Test-only host budget simulation. Never included in the database image.
 * This changes the detected budget, not the host's actual memory allowance.
 */
#define _GNU_SOURCE
#include <dlfcn.h>
#include <stdlib.h>
#include <unistd.h>
long sysconf(int name){
 static long (*real_sysconf)(int);if(!real_sysconf)real_sysconf=dlsym(RTLD_NEXT,"sysconf");
 const char *limit=getenv("MV_TEST_MEMORY_MB");
 if(limit&&name==_SC_PHYS_PAGES)return strtol(limit,0,10)*1024*1024/real_sysconf(_SC_PAGESIZE);
 return real_sysconf(name);
}
