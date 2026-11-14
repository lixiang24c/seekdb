#include <cassert>
#include <iostream>
#include <map>
#include <memory>
#include "../native/queryable_log/resource_budget.hpp"
#include "../native/queryable_log/paged_directory.hpp"
struct Item {uint64_t n=0;uint64_t weight()const{return n;}bool operator==(const Item &x)const{return n==x.n;}};
int main(){
    using Files=std::map<std::string,std::string>;size_t cases=0;
    auto check=[&](Files files,uint64_t physical,uint64_t expected){auto read=[&](const std::string &p){auto i=files.find(p);return i==files.end()?std::string():i->second;};assert(cppdb::memory_limit(read,physical)==expected);++cases;};
    const uint64_t MiB=1u<<20,physical=2ull<<30;
    check({},physical,16*MiB);check({},0,16*MiB);
    check({{"/sys/fs/cgroup/memory.max","33554432"}},physical,32*MiB);
    check({{"/sys/fs/cgroup/memory.max","0"}},physical,0);
    check({{"/sys/fs/cgroup/memory.max","garbage"}},physical,16*MiB);
    Files f={{"/proc/self/cgroup","0::/parent/child\n"},{"/proc/self/mountinfo","1 0 0:1 / /custom rw - cgroup2 cgroup rw\n"},{"/custom/parent/child/memory.max","134217728"},{"/custom/parent/memory.max","67108864"},{"/custom/memory.max","max"}};
    check(f,physical,64*MiB);f["/custom/parent/memory.max"]="max";check(f,physical,128*MiB);
    f["/custom/parent/child/memory.max"]="max";check(f,physical,physical);
    f["/proc/self/cgroup"]="0::/\n";f["/proc/self/mountinfo"]="1 0 0:1 /host/container /custom rw - cgroup2 cgroup rw\n";f["/custom/memory.max"]="134217728";check(f,physical,128*MiB);
    f["/proc/self/cgroup"]="0::/host/container\n";check(f,physical,128*MiB);
    f["/proc/self/cgroup"]="0::/../outside\n";check(f,physical,16*MiB);
    check({{"/proc/self/cgroup","4:memory:/a/b\n"},{"/proc/self/mountinfo","1 0 0:1 / /mem rw - cgroup cgroup rw,memory\n"},{"/mem/a/b/memory.limit_in_bytes","134217728"},{"/mem/a/memory.limit_in_bytes","33554432"}},physical,32*MiB);
    check({{"/proc/self/cgroup","3:cpu,memory:/a\n"},{"/proc/self/mountinfo","1 0 0:1 / /mem rw - cgroup cgroup rw,cpu,memory\n"},{"/mem/a/memory.limit_in_bytes","9223372036854771712"}},physical,physical);
    check({{"/proc/self/cgroup","0::/a\n"},{"/proc/self/mountinfo","1 0 0:1 / /my\\040group rw - cgroup2 cgroup rw\n"},{"/my group/a/memory.max","16777216"}},physical,16*MiB);
    check({{"/proc/self/cgroup","0::/a\n"},{"/proc/self/mountinfo","1 0 0:1 / /mem rw - cgroup2 cgroup rw\n"},{"/mem/a/memory.max","8589934592"}},physical,physical);
    PagedDirectory<Item> a;for(size_t i=0;i<257;i++)a.push_back({i});auto b=a;
    assert(a.shares_page(b,0)&&a.shares_page(b,256));auto total=a.weight();b.edit(65).n=1000;
    assert(a[65].n==65&&b[65].n==1000&&!a.shares_page(b,65)&&a.shares_page(b,0));
    assert(b.weight()==total-65+1000);b.resize(63);b.push_back({999});assert(a.size()==257&&a[63].n==63&&b[63].n==999);
    std::cout<<"{\"status\":\"PASS\",\"resource_layouts\":"<<cases<<",\"paged_copy_on_write\":true}\n";
}
