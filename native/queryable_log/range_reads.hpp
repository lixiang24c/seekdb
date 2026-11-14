#pragma once
struct LogRangeReads {
    struct Run {size_t first,last;};
    std::array<Run,8> runs{};size_t size=0;
    bool plan(const unsigned char *mask,size_t count,size_t stride,uint64_t offset){
        if(!mask||!count||!stride)return false;
        for(size_t i=0;i<count;i++)if(mask[i]){
            if(size&&(i-runs[size-1].last)*stride<=4096)runs[size-1].last=i+1;
            else{if(size==runs.size())return false;runs[size++]={i,i+1};}
        }
        if(!size)return false;
        uint64_t pages=0,last_end=0,bytes=0;
        for(size_t i=0;i<size;i++){
            auto &r=runs[i];uint64_t first=(offset+r.first*stride)/4096,end=(offset+r.last*stride-1)/4096+1;
            pages+=end-std::max(first,last_end);last_end=end;bytes+=(r.last-r.first)*stride;
        }
        uint64_t total_pages=(offset+count*stride-1)/4096-offset/4096+1;
        return bytes<=count*stride&&count*stride-bytes>=65536&&pages<=total_pages/4;
    }
};
