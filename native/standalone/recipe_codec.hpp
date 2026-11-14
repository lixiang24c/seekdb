#pragma once
#include "storage.hpp"
namespace cppdb {
// The materialized record layout stays unchanged. Only a bounded incoming
// recipe batch can share a byte suffix of its scalar records.
inline std::optional<Bytes> scalar_suffix_pack(const Bytes &input,size_t dimension,size_t count){
    if(count<4||count>8192||input.size()>(1u<<20)||input.size()<8192)return {};
    size_t width=dimension*4,pos=0,common=0;const unsigned char *tail=nullptr;
    for(size_t i=0;i<count;i++){
        if(pos>input.size()||input.size()-pos<12)throw std::runtime_error("incomplete recipe header");
        uint32_t n;std::memcpy(&n,input.data()+pos+8,4);pos+=12;
        if(n>input.size()-pos||width>input.size()-pos-n)throw std::runtime_error("incomplete recipe columns");
        auto end=input.data()+pos+n;
        if(!i){tail=end;common=std::min(size_t(n),size_t(8192));}
        else{
            common=std::min(common,size_t(n));
            if(common&&std::memcmp(tail-common,end-common,common)){
                size_t same=0;while(same<common&&tail[-1-ptrdiff_t(same)]==end[-1-ptrdiff_t(same)])++same;common=same;
            }
            if(!common)return {};
        }
        pos+=n+width;
    }
    if(pos!=input.size())throw std::runtime_error("trailing recipe columns");
    size_t saving=(count-1)*common;if(saving<8||saving-8<std::max(size_t(4096),input.size()/16))return {};
    Bytes out;out.reserve(input.size()-saving+8);::put(out,uint32_t(count));::put(out,uint32_t(common));out.insert(out.end(),tail-common,tail);pos=0;
    for(size_t i=0;i<count;i++){
        int64_t id;uint32_t n;std::memcpy(&id,input.data()+pos,8);std::memcpy(&n,input.data()+pos+8,4);pos+=12;
        ::put(out,id);::put(out,uint32_t(n-common));out.insert(out.end(),input.begin()+pos,input.begin()+pos+n-common);
        out.insert(out.end(),input.begin()+pos+n,input.begin()+pos+n+width);pos+=n+width;
    }
    return out;
}
inline Bytes scalar_suffix_unpack(const Bytes &input,size_t dimension){
    if(input.size()<8)throw std::runtime_error("incomplete suffix recipe");
    uint32_t count,shared;std::memcpy(&count,input.data(),4);std::memcpy(&shared,input.data()+4,4);
    if(count<4||count>8192||!shared||shared>8192||shared>input.size()-8)throw std::runtime_error("invalid suffix recipe");
    uint64_t total=input.size()-8-shared+uint64_t(count)*shared;
    if(total>(1u<<20))throw std::runtime_error("oversized decoded recipe");
    Bytes out;out.reserve(total);size_t pos=8+shared,width=dimension*4;
    for(size_t i=0;i<count;i++){
        if(pos>input.size()||input.size()-pos<12)throw std::runtime_error("incomplete suffix row");
        int64_t id;uint32_t prefix;std::memcpy(&id,input.data()+pos,8);std::memcpy(&prefix,input.data()+pos+8,4);pos+=12;
        if(prefix>input.size()-pos||width>input.size()-pos-prefix||uint64_t(prefix)+shared>UINT32_MAX)throw std::runtime_error("invalid suffix columns");
        ::put(out,id);::put(out,uint32_t(prefix+shared));out.insert(out.end(),input.begin()+pos,input.begin()+pos+prefix);
        out.insert(out.end(),input.begin()+8,input.begin()+8+shared);pos+=prefix;out.insert(out.end(),input.begin()+pos,input.begin()+pos+width);pos+=width;
    }
    if(pos!=input.size()||out.size()!=total)throw std::runtime_error("suffix recipe length mismatch");return out;
}
}
