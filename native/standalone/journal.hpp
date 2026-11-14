#pragma once
#include "recipe_codec.hpp"
#include <sys/types.h>
namespace cppdb {
struct Descriptor {
    int fd=-1;explicit Descriptor(int value=-1):fd(value){}
    Descriptor(const Descriptor&)=delete;Descriptor&operator=(const Descriptor&)=delete;
    ~Descriptor(){if(fd>=0)::close(fd);}
    int release(){int out=fd;fd=-1;return out;}
};
inline void write_all(int fd,const void *data,size_t size){
    const auto *p=static_cast<const unsigned char*>(data);while(size){auto n=::write(fd,p,size);if(n<0&&errno==EINTR)continue;if(n<=0)::error("commit write");p+=n;size-=n;}
}
inline void read_at(int fd,void *data,size_t size,uint64_t offset){
    auto *p=static_cast<unsigned char*>(data);while(size){auto n=::pread(fd,p,size,offset);if(n<0&&errno==EINTR)continue;if(n<=0)throw std::runtime_error("incomplete commit stream");p+=n;size-=n;offset+=n;}
}
inline uint32_t crc_bytes(uint32_t crc,const void *data,size_t size){
    auto *p=static_cast<const unsigned char*>(data);while(size){auto n=std::min(size,size_t(UINT_MAX));crc=::crc32(crc,p,n);p+=n;size-=n;}return crc;
}
struct Recipe {Part output;size_t dimension=0;std::vector<Part> sources;Bytes payload;std::string encoding;};
class Journal {
    std::filesystem::path root,path;int fd=-1,dirfd=-1;
    static constexpr size_t HEADER=12,TRAILER=12,CHUNK=65536;
    std::filesystem::path safe(const std::string &name)const{
        if(name.rfind("mv-",0)||std::filesystem::path(name).filename()!=name)throw std::runtime_error("invalid segment path");return root/name;
    }
    uint64_t frame(int target,const Catalog &tables,const Catalog &previous,const std::vector<Recipe> &recipes){
        auto names=catalog_files(tables),old=catalog_files(previous);std::set<std::string> covered;Json described=Json::array();
        for(auto &r:recipes){
            if(!names.count(r.output.file)||old.count(r.output.file))continue;
            Json j=Json::object();j["row"]=r.output.file;j["vector"]=r.output.vectors;j["dimension"]=r.dimension;j["bytes"]=r.payload.size();if(!r.encoding.empty())j["encoding"]=r.encoding;Json sources=Json::array();
            for(auto &p:r.sources){Json x=Json::array();x.push(p.file);x.push(p.vectors);x.push(p.count);Json ex=Json::array();for(auto id:p.excluded)ex.push(id);x.push(ex);sources.push(x);}j["sources"]=sources;described.push(j);covered.insert(r.output.file);covered.insert(r.output.vectors);
        }
        Json files=Json::array(),sizes=Json::object();uint64_t payload_bytes=0,live=0;
        for(auto &name:names){size_t n=std::filesystem::file_size(safe(name));sizes[name]=n;live+=n;
            if(!old.count(name)&&!covered.count(name)){Json x=Json::array();x.push(name);x.push(n);files.push(x);payload_bytes+=n;}}
        for(auto &j:described.items())payload_bytes+=j.at("bytes").integer();
        Json doc=Json::object();doc["tables"]=catalog_json(tables);doc["files"]=files;doc["recipes"]=described;doc["sizes"]=sizes;std::string meta=doc.dump();
        if(meta.size()>UINT32_MAX||payload_bytes>UINT64_MAX-meta.size())throw std::runtime_error("commit frame too large");
        uint64_t size=payload_bytes+meta.size();uint32_t meta_size=meta.size();unsigned char head[12];std::memcpy(head,&size,8);std::memcpy(head+8,&meta_size,4);
        uint32_t crc=crc_bytes(0,head,12);write_all(target,head,12);write_all(target,meta.data(),meta.size());crc=crc_bytes(crc,meta.data(),meta.size());
        for(auto &r:recipes)if(names.count(r.output.file)&&!old.count(r.output.file)){
            write_all(target,r.payload.data(),r.payload.size());crc=crc_bytes(crc,r.payload.data(),r.payload.size());
        }
        unsigned char chunk[CHUNK];for(auto &item:files.items()){
            auto name=item.items()[0].string();size_t left=item.items()[1].integer();::File file(safe(name).c_str(),"rb");
            while(left){size_t n=std::min(left,CHUNK);file.read(chunk,n);write_all(target,chunk,n);crc=crc_bytes(crc,chunk,n);left-=n;}
        }
        unsigned char tail[12];std::memcpy(tail,&crc,4);std::memcpy(tail+4,"MVCMT001",8);write_all(target,tail,12);
        if(::fsync(target))::error("commit sync");::posix_fadvise(target,0,0,POSIX_FADV_DONTNEED);return live;
    }
    // Process-crash recovery reuses complete materializations by size/header.
    // Same-size body corruption and loss of dirty pages on machine failure are
    // outside this policy; WAL frame CRC and commit fsync remain mandatory.
    bool materialized(const Json &doc){
        if(!doc.contains("sizes"))return false;
        for(auto &[name,size]:doc.at("sizes").fields()){
            Descriptor file(::open(safe(name).c_str(),O_RDONLY));if(file.fd<0){if(errno==ENOENT)return false;::error("open committed segment");}
            struct stat st;if(::fstat(file.fd,&st))::error("stat committed segment");if(st.st_size!=size.integer())return false;
            char magic[8];if(::pread(file.fd,magic,8,0)!=8)return false;
            if(std::memcmp(magic,"MVROW001",8)&&std::memcmp(magic,"MVFLT001",8)&&std::memcmp(magic,"MVKEY001",8)&&std::memcmp(magic,"MVKEY002",8))return false;
        }return true;
    }
    void collect(const Catalog &tables){
        auto keep=catalog_files(tables);for(auto &p:std::filesystem::directory_iterator(root))if(p.is_regular_file()){
            auto name=p.path().filename().string();if(name.rfind("mv-",0)==0&&!keep.count(name))std::filesystem::remove(p.path());
        }
    }
    void restore(const Json &doc,uint64_t offset){
        if(doc.contains("recipes"))for(auto &r:doc.at("recipes").items()){
            size_t n=r.at("bytes").integer();Bytes payload(n);if(n)read_at(fd,payload.data(),n,offset);offset+=n;
            if(r.contains("encoding")){if(r.at("encoding").string()!="scalar-suffix-v1")throw std::runtime_error("unsupported recipe encoding");payload=scalar_suffix_unpack(payload,r.at("dimension").integer());}
            std::vector<mv_source> sources;std::vector<std::vector<int64_t>> excluded;std::vector<std::string> source_rows,source_vectors;
            size_t count=r.at("sources").items().size();sources.reserve(count);excluded.reserve(count);source_rows.reserve(count);source_vectors.reserve(count);
            for(auto &item:r.at("sources").items()){
                auto &a=item.items();source_rows.push_back(safe(a[0].string()).string());source_vectors.push_back(safe(a[1].string()).string());excluded.emplace_back();for(auto &x:a[3].items())excluded.back().push_back(x.integer());
                sources.push_back({source_rows.back().c_str(),source_vectors.back().c_str(),uint64_t(a[2].integer()),excluded.back().data(),excluded.back().size()});
            }
            auto rows=safe(r.at("row").string()),vectors=safe(r.at("vector").string()),keys=root/"journal-recovery.keys";
            for(auto &p:{rows,vectors,keys})std::filesystem::remove(p);mv_segment result;char message[512];
            if(mv_storage_merge(rows.c_str(),vectors.c_str(),keys.c_str(),sources.data(),sources.size(),payload.data(),payload.size(),r.at("dimension").integer(),0,&result,message,sizeof(message)))throw std::runtime_error(message);
        }
        unsigned char chunk[CHUNK];for(auto &item:doc.at("files").items()){
            auto &a=item.items();::File output(safe(a[0].string()).c_str(),"wb");size_t left=a[1].integer();
            while(left){size_t n=std::min(left,CHUNK);read_at(fd,chunk,n,offset);output.write(chunk,n);offset+=n;left-=n;}output.close();
        }
    }
public:
    Catalog tables;
    explicit Journal(std::filesystem::path directory):root(std::move(directory)),path(root/"commit.log"){
        bool fresh=!std::filesystem::exists(path);if(fresh&&std::filesystem::exists(root/"manifest.json"))throw std::runtime_error("native prototype requires a fresh data directory");fd=::open(path.c_str(),O_RDWR|O_CREAT|O_APPEND,0666);if(fd<0)::error("open commit log");
        dirfd=::open(root.c_str(),O_RDONLY|O_DIRECTORY);if(dirfd<0){::close(fd);fd=-1;::error("open data directory");}
        try{
            if(fresh){if(::fsync(fd)||::fsync(dirfd))::error("persist log name");}
            recover();
        }catch(...){::close(fd);::close(dirfd);fd=dirfd=-1;throw;}
    }
    ~Journal(){if(fd>=0)::close(fd);if(dirfd>=0)::close(dirfd);}
    void recover(){
        struct stat st;if(::fstat(fd,&st))::error("stat commit log");uint64_t length=st.st_size,offset=0,valid=0;Json latest;unsigned char chunk[CHUNK];
        while(offset<=length&&length-offset>=HEADER){
            unsigned char head[12];read_at(fd,head,12,offset);uint64_t size;uint32_t meta_size;std::memcpy(&size,head,8);std::memcpy(&meta_size,head+8,4);
            if(meta_size>size||length-offset<HEADER+TRAILER||size>length-offset-HEADER-TRAILER)break;
            std::string meta(meta_size,'\0');read_at(fd,meta.data(),meta.size(),offset+HEADER);uint32_t crc=crc_bytes(crc_bytes(0,head,HEADER),meta.data(),meta.size());
            uint64_t at=offset+HEADER+meta_size,left=size-meta_size;while(left){size_t n=std::min(left,uint64_t(CHUNK));read_at(fd,chunk,n,at);crc=crc_bytes(crc,chunk,n);at+=n;left-=n;}
            unsigned char tail[12];read_at(fd,tail,12,offset+HEADER+size);uint32_t expected;std::memcpy(&expected,tail,4);if(expected!=crc||std::memcmp(tail+4,"MVCMT001",8))break;
            latest=Json::parse(meta);valid=offset+HEADER+size+TRAILER;offset=valid;
        }
        if(!latest.is_null()){
            if(!materialized(latest)){
                offset=0;while(offset<valid){unsigned char head[12];read_at(fd,head,12,offset);uint64_t size;uint32_t n;std::memcpy(&size,head,8);std::memcpy(&n,head+8,4);std::string meta(n,'\0');read_at(fd,meta.data(),n,offset+HEADER);Json doc=Json::parse(meta);restore(doc,offset+HEADER+n);collect(catalog_from(doc.at("tables")));offset+=HEADER+size+TRAILER;}
            }
            tables=catalog_from(latest.at("tables"));
        }
        if(::ftruncate(fd,valid))::error("truncate partial commit");
        std::filesystem::remove(root/"commit.next");std::filesystem::remove(root/"journal-recovery.keys");collect(tables);
    }
    void commit(const Catalog &current,const Catalog &previous,const std::vector<Recipe> &recipes={}){
        uint64_t live=frame(fd,current,previous,recipes);auto bytes=::lseek(fd,0,SEEK_END);if(bytes<0)::error("tell commit log");
        if(uint64_t(bytes)>std::max(uint64_t(8<<20),live*4))checkpoint(current);
    }
    void checkpoint(const Catalog &current){
        auto temp=root/"commit.next";Descriptor replacement(::open(temp.c_str(),O_RDWR|O_CREAT|O_TRUNC|O_APPEND,0666));if(replacement.fd<0)::error("open checkpoint");
        frame(replacement.fd,current,{},{});if(::rename(temp.c_str(),path.c_str()))::error("publish checkpoint");
        int old=fd;fd=replacement.release();::close(old);if(::fsync(dirfd))::error("persist checkpoint name");
    }
};
}
