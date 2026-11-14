#pragma once
// One admitted column at a time. Codes address physical ordinals, including
// old versions; the query must still intersect its visibility mask.
struct LogIntColumn {
    const LogTable *owner=nullptr;size_t column=0,requests=0,distinct=0;
    bool attempted=false,ready=false;
    std::array<int64_t,256> dictionary{};Bytes codes;
    void clear(){owner=nullptr;column=requests=distinct=0;attempted=ready=false;Bytes().swap(codes);}
    bool request(const LogTable &table,size_t col){
        if(owner!=&table||column!=col){clear();owner=&table;column=col;}
        ++requests;return ready||(!attempted&&requests>=2);
    }
    bool append(int64_t value){
        size_t code=0;while(code<distinct&&dictionary[code]!=value)++code;
        if(code==distinct){if(distinct==dictionary.size())return false;dictionary[distinct++]=value;}
        codes.push_back(static_cast<unsigned char>(code));return true;
    }
};
