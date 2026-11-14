#pragma once
#include <algorithm>
#include <charconv>
#include <cstdint>
#include <fstream>
#include <sstream>
#include <string>
#include <vector>
#include <unistd.h>

namespace cppdb {
// Probe capabilities/files, never kernel release numbers or optional stat keys.
// Read is injectable so namespace layouts can be checked without host mounts.
template<class Read> uint64_t memory_limit(Read read,uint64_t physical) {
    uint64_t limit=UINT64_MAX;bool resolved=false;
    auto value=[&](const std::string &file){
        std::istringstream input(read(file));std::string word;input>>word;
        if(word=="max")return true;
        uint64_t n=0;auto r=std::from_chars(word.data(),word.data()+word.size(),n);
        if(word.empty()||r.ec!=std::errc()||r.ptr!=word.data()+word.size())return false;
        if(n<(uint64_t(1)<<60))limit=std::min(limit,n);
        return true;
    };
    auto decode=[](std::string s){
        std::string out;
        for(size_t i=0;i<s.size();++i){
            if(s[i]=='\\'&&i+3<s.size()&&s[i+1]>='0'&&s[i+1]<='7'&&s[i+2]>='0'&&s[i+2]<='7'&&s[i+3]>='0'&&s[i+3]<='7'){
                out+=char((s[i+1]-'0')*64+(s[i+2]-'0')*8+s[i+3]-'0');i+=3;
            }else out+=s[i];
        }return out;
    };
    struct Group {std::string path;bool v2;};std::vector<Group> groups;
    std::istringstream memberships(read("/proc/self/cgroup"));std::string line;
    while(std::getline(memberships,line)){
        auto a=line.find(':'),b=line.find(':',a==line.npos?a:a+1);
        if(a==line.npos||b==line.npos)continue;
        auto controllers=line.substr(a+1,b-a-1);
        if(controllers.empty()||(","+controllers+",").find(",memory,")!=line.npos)groups.push_back({line.substr(b+1),controllers.empty()});
    }
    std::istringstream mounts(read("/proc/self/mountinfo"));
    while(std::getline(mounts,line)){
        auto split=line.find(" - ");if(split==line.npos)continue;
        std::istringstream left(line.substr(0,split)),right(line.substr(split+3));
        std::string id,parent,device,root,mount,options,fs,source,super;
        if(!(left>>id>>parent>>device>>root>>mount>>options)||!(right>>fs>>source>>super))continue;
        bool v2=fs=="cgroup2";if(!v2&&(fs!="cgroup"||(","+super+",").find(",memory,")==line.npos))continue;
        root=decode(root);mount=decode(mount);if(mount=="/")mount.clear();
        for(auto &g:groups){
            if(g.v2!=v2||g.path.empty()||g.path[0]!='/')continue;
            std::string relative=g.path;
            if((relative+"/").find("/../")!=relative.npos)continue;
            if(root!="/"&&(relative==root||relative.compare(0,root.size()+1,root+"/")==0))relative=relative.substr(root.size());
            // A cgroup namespace can expose '/' while mountinfo names its host root.
            std::string current=mount+relative;if(current.size()>1&&current.back()=='/')current.pop_back();
            const char *leaf=v2?"/memory.max":"/memory.limit_in_bytes";
            bool readable=value(current+leaf);resolved=resolved||readable;
            while(current.size()>mount.size()){
                auto slash=current.rfind('/');if(slash==current.npos||slash<mount.size())break;
                current.resize(slash);value(current+leaf);
            }
        }
    }
    // Conventional container mounts also work when proc mount information is hidden.
    value("/sys/fs/cgroup/memory.max");value("/sys/fs/cgroup/memory/memory.limit_in_bytes");
    if(limit!=UINT64_MAX)return physical?std::min(limit,physical):limit;
    if(resolved&&physical)return physical;
    // Unknown is not an unlimited allocation license, nor a zero mmap budget.
    return physical?std::min(physical,uint64_t(16u<<20)):uint64_t(16u<<20);
}
inline uint64_t memory_limit(){
    auto read=[](const std::string &path){std::ifstream in(path);std::ostringstream out;if(in)out<<in.rdbuf();return out.str();};
    long pages=::sysconf(_SC_PHYS_PAGES),size=::sysconf(_SC_PAGESIZE);
    uint64_t physical=pages>0&&size>0&&uint64_t(pages)<=UINT64_MAX/uint64_t(size)?uint64_t(pages)*uint64_t(size):0;
    return memory_limit(read,physical);
}
}
