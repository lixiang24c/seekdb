#pragma once
#include "../standalone/engine.hpp"
#include <tuple>
extern "C" int mv_screen_begin_query(const unsigned char*,size_t,size_t,const double*,size_t);
extern "C" void mv_screen_end_query(void);
struct mv_record_span {const unsigned char *data,*mask;size_t count;const unsigned char *quant=nullptr;const uint32_t *indices=nullptr;size_t index_count=0,index_base=0;};
extern "C" int mv_quant_available(void);
extern "C" int mv_screen_simd(void);
extern "C" uint64_t mv_quant_scan_calls(void);
extern "C" int mv_quant_pack(const unsigned char*,size_t,unsigned char*,double*,double*);
extern "C" const char *mv_block_kernel(void);
extern "C" int mv_quant_prepare(const double*,double,double,const unsigned char*);
extern "C" size_t mv_screen_topk_spans(const mv_record_span*,size_t,size_t,const double*,double,size_t,uint32_t*);

extern "C" size_t mv_refine_block_candidates(const unsigned char*,const uint64_t*,const double*,size_t,uint32_t*,size_t,double*,int*);
extern "C" size_t mv_quant_topk_bound_blocks(const unsigned char*,size_t,const double*,const uint32_t*,size_t,size_t,uint32_t*);
extern "C" uint64_t mv_tile_bound_calls(void);
extern "C" uint64_t mv_sparse_scan_calls(void);
extern "C" uint64_t mv_sparse_zero_calls(void);
extern "C" uint64_t mv_sparse_pair_total(void);
extern "C" uint64_t mv_refine_four_calls(void);
extern "C" uint64_t mv_refine_four_points(void);
extern "C" uint64_t mv_fp64_bound_calls(void);
extern "C" uint64_t mv_legacy_bound_calls(void);
extern "C" int mv_quant_pack_block_row(const unsigned char*,unsigned char*,size_t,double*,double*);
extern "C" size_t mv_quant_topk_blocks(const unsigned char*,size_t,const uint32_t*,size_t,size_t,uint32_t*);

#include "range_directory.hpp"
#include "range_reads.hpp"
#include "resource_budget.hpp"
#include "paged_directory.hpp"

// Architectural prototype only. Independently framed queryable data followed
// by a catalog commit, all in one append stream. Not a submission engine yet.
namespace cppdb {
struct LogExtent {
    uint64_t rows=0,row_bytes=0,vectors=0,vector_bytes=0,count=0,keys=0;
    int64_t minimum=0,maximum=0;std::vector<uint64_t> excluded;
    uint64_t weight()const{return count?(row_bytes/count+vector_bytes/count)*(count-excluded.size()):0;}
    bool visible(size_t ordinal)const{return !std::binary_search(excluded.begin(),excluded.end(),ordinal);}
    bool operator==(const LogExtent &other)const{return std::tie(rows,row_bytes,vectors,vector_bytes,count,keys,minimum,maximum)==std::tie(other.rows,other.row_bytes,other.vectors,other.vector_bytes,other.count,other.keys,other.minimum,other.maximum)&&excluded==other.excluded;}
    Json json()const{Json out=Json::array();for(auto v:{rows,row_bytes,vectors,vector_bytes,count,keys})out.push(size_t(v));out.push(minimum);out.push(maximum);Json hidden=Json::array();for(auto ordinal:excluded)hidden.push(size_t(ordinal));out.push(hidden);return out;}
    static LogExtent from(const Json &json){auto &a=json.items();if(a.size()!=8&&a.size()!=9)throw std::runtime_error("invalid extent metadata");LogExtent out;uint64_t *fields[]={&out.rows,&out.row_bytes,&out.vectors,&out.vector_bytes,&out.count,&out.keys};for(size_t i=0;i<6;i++){auto n=a[i].integer();if(n<0)throw std::runtime_error("negative extent offset");*fields[i]=n;}out.minimum=a[6].integer();out.maximum=a[7].integer();if(a.size()==9)for(auto &v:a[8].items()){auto ordinal=v.integer();if(ordinal<0||uint64_t(ordinal)>=out.count||(!out.excluded.empty()&&uint64_t(ordinal)<=out.excluded.back()))throw std::runtime_error("invalid visibility ordinal");out.excluded.push_back(ordinal);}return out;}
};
#include "predicate.hpp"
#include "block_scan.hpp"
struct LogTable {Table schema;::PagedDirectory<LogExtent> extents;};
#include "int_column.hpp"
using LogCatalog=std::map<std::string,LogTable>;
inline Json log_catalog_json(const LogCatalog &tables){
    Json out=Json::object();for(auto &[name,table]:tables){Json value=Json::object(),extents=Json::array();value["schema"]=table.schema.json();for(auto &extent:table.extents)extents.push(extent.json());value["extents"]=extents;out[name]=value;}return out;
}
inline LogCatalog log_catalog_from(const Json &json){
    LogCatalog out;for(auto &[name,value]:json.fields()){LogTable table;table.schema=Table::from(value.at("schema"));for(auto &v:value.at("extents").items())table.extents.push_back(LogExtent::from(v));out.emplace(name,std::move(table));}return out;
}
inline Json log_catalog_delta(const LogCatalog &tables,const LogCatalog &previous){
    Json changes=Json::object();
    for(auto &[name,table]:tables){
        auto old=previous.find(name);Json schema=table.schema.json(),patches=Json::array();
        for(size_t i=0;i<table.extents.size();){
            size_t stop=std::min(table.extents.size(),(i/64+1)*64);
            if(old!=previous.end()&&table.extents.shares_page(old->second.extents,i)){i=stop;continue;}
            for(;i<stop;i++)if(old==previous.end()||i>=old->second.extents.size()||!(table.extents[i]==old->second.extents[i])){Json patch=Json::array();patch.push(i);patch.push(table.extents[i].json());patches.push(std::move(patch));}
        }
        if(old!=previous.end()&&patches.items().empty()&&table.extents.size()==old->second.extents.size()&&schema.dump()==old->second.schema.json().dump())continue;
        Json change=Json::object();change["schema"]=schema;change["length"]=table.extents.size();change["patches"]=std::move(patches);changes[name]=std::move(change);
    }
    for(auto &[name,_]:previous)if(!tables.count(name))changes[name]=nullptr;return changes;
}
inline void log_catalog_apply(LogCatalog &tables,const Json &changes){
    for(auto &[name,change]:changes.fields()){
        if(change.is_null()){tables.erase(name);continue;}
        if(change.contains("patches")){
            auto length=change.at("length").integer();if(length<0)throw std::runtime_error("negative directory length");auto &table=tables[name];
            if(uint64_t(length)<table.extents.size())table.extents.resize(length);
            size_t last=0;bool first=true;
            for(auto &entry:change.at("patches").items()){
                auto &patch=entry.items();if(patch.size()!=2)throw std::runtime_error("invalid directory patch");auto index=patch[0].integer();
                if(index<0||index>=length||(!first&&uint64_t(index)<=last)||uint64_t(index)>table.extents.size())throw std::runtime_error("invalid directory patch position");
                last=index;first=false;auto extent=LogExtent::from(patch[1]);
                if(uint64_t(index)==table.extents.size())table.extents.push_back(std::move(extent));else table.extents.edit(index)=std::move(extent);
            }
            if(table.extents.size()!=uint64_t(length))throw std::runtime_error("incomplete directory patches");table.schema=Table::from(change.at("schema"));continue;
        }
        auto keep=change.at("keep").integer();if(keep<0)throw std::runtime_error("negative catalog prefix");
        auto found=tables.find(name);if(found==tables.end()){if(keep)throw std::runtime_error("missing catalog prefix");found=tables.emplace(name,LogTable{}).first;}
        auto &table=found->second;if(uint64_t(keep)>table.extents.size())throw std::runtime_error("invalid catalog prefix");table.extents.resize(keep);
        for(auto &extent:change.at("append").items())table.extents.push_back(LogExtent::from(extent));table.schema=Table::from(change.at("schema"));
    }
}
class QueryableLog {
    std::filesystem::path directory,path;Descriptor fd;uint64_t confirmed=0;
    static std::filesystem::path prepare(std::filesystem::path root){
        std::filesystem::create_directories(root);if(std::filesystem::exists(root/"commit.log"))throw std::runtime_error("queryable-log prototype requires an independent fresh directory");
        for(auto &p:std::filesystem::directory_iterator(root))if(p.path().filename().string().rfind("query-result-",0)==0)std::filesystem::remove(p.path());return root;
    }
    struct Frame {
        int fd;uint32_t crc;Bytes pending;
        Frame(int descriptor,uint64_t body,const std::string &meta):fd(descriptor){
            if(meta.size()>UINT32_MAX||body>UINT64_MAX-meta.size())throw std::runtime_error("frame size overflow");
            unsigned char header[12];uint64_t size=body+meta.size();uint32_t n=meta.size();std::memcpy(header,&size,8);std::memcpy(header+8,&n,4);
            crc=crc_bytes(crc_bytes(0,header,12),meta.data(),meta.size());write_all(fd,header,12);write_all(fd,meta.data(),meta.size());pending.reserve(65536);
        }
        void put(const void *data,size_t n){
            auto *p=static_cast<const unsigned char*>(data);
            while(n){size_t take=std::min(n,65536-pending.size());pending.insert(pending.end(),p,p+take);p+=take;n-=take;if(pending.size()==65536){crc=crc_bytes(crc,pending.data(),pending.size());write_all(fd,pending.data(),pending.size());pending.clear();}}
        }
        template<class T>void integer(T n){put(&n,sizeof(n));}
        void finish(){if(!pending.empty()){crc=crc_bytes(crc,pending.data(),pending.size());write_all(fd,pending.data(),pending.size());}unsigned char tail[12];std::memcpy(tail,&crc,4);std::memcpy(tail+4,"MVCMT001",8);write_all(fd,tail,12);}
    };
public:
    LogCatalog recovered;
    explicit QueryableLog(std::filesystem::path root):directory(prepare(std::move(root))),path(directory/"queryable.log"),fd(::open(path.c_str(),O_RDWR|O_CREAT|O_APPEND,0666)){
        if(fd.fd<0)::error("open queryable log");Descriptor dir(::open(directory.c_str(),O_RDONLY|O_DIRECTORY));if(dir.fd<0)::error("open log directory");if(::fsync(fd.fd)||::fsync(dir.fd))::error("persist queryable log name");recover();
    }
    const std::filesystem::path &root()const{return directory;}
    int descriptor()const{return fd.fd;}
    uint64_t size()const{struct stat st;if(::fstat(fd.fd,&st))::error("stat queryable log");return st.st_size;}
    void recover(){
        uint64_t length=size(),offset=0;unsigned char buffer[65536];
        while(offset<=length&&length-offset>=24){
            unsigned char head[12];read_at(fd.fd,head,12,offset);uint64_t bytes;uint32_t n;std::memcpy(&bytes,head,8);std::memcpy(&n,head+8,4);
            if(n>bytes||bytes>length-offset-24)break;
            std::string meta(n,'\0');read_at(fd.fd,meta.data(),n,offset+12);uint32_t crc=crc_bytes(crc_bytes(0,head,12),meta.data(),n);
            uint64_t left=bytes-n,at=offset+12+n;while(left){size_t chunk=std::min(left,uint64_t(sizeof(buffer)));read_at(fd.fd,buffer,chunk,at);crc=crc_bytes(crc,buffer,chunk);at+=chunk;left-=chunk;}
            unsigned char tail[12];read_at(fd.fd,tail,12,offset+12+bytes);uint32_t expected;std::memcpy(&expected,tail,4);if(expected!=crc||std::memcmp(tail+4,"MVCMT001",8))break;
            Json parsed=Json::parse(meta);if(parsed.at("kind").string()=="catalog"){recovered=log_catalog_from(parsed.at("tables"));confirmed=offset+24+bytes;}
            else if(parsed.at("kind").string()=="delta"){log_catalog_apply(recovered,parsed.at("changes"));confirmed=offset+24+bytes;}
            else if(parsed.at("kind").string()!="data")throw std::runtime_error("unknown queryable log frame");offset+=24+bytes;
        }
        for(auto &[_,table]:recovered){uint64_t count=0;for(auto &e:table.extents){
            if(e.rows>confirmed||e.row_bytes>confirmed-e.rows||e.vectors>confirmed||e.vector_bytes>confirmed-e.vectors||e.row_bytes<16||e.keys>e.row_bytes-8||e.count>(e.row_bytes-e.keys-8)/16||e.vector_bytes<8||e.count>(e.vector_bytes-8)/(8+table.schema.dimension()*4)||e.count>UINT64_MAX-count)throw std::runtime_error("extent outside committed prefix");count+=e.count-e.excluded.size();
        }if(count!=table.schema.count)throw std::runtime_error("catalog extent count mismatch");}
        if(::ftruncate(fd.fd,confirmed))::error("truncate uncommitted queryable data");
        // Only the installed log is authoritative; a leftover checkpoint is disposable.
        std::filesystem::remove(directory/"queryable.next");
    }
    LogExtent append(const Bytes &batch,size_t dimension,size_t count,int output=-1){
        int target=output<0?fd.fd:output;struct stat target_stat;if(::fstat(target,&target_stat))::error("stat append target");
        LogExtent extent;extent.count=count;uint64_t scalar_bytes=0;size_t width=dimension*4;
        auto each=[&](auto consume){size_t pos=0,seen=0;int64_t previous=0;
            while(pos<batch.size()){
                if(batch.size()-pos<12)throw std::runtime_error("incomplete inserted row");int64_t id;uint32_t n;std::memcpy(&id,batch.data()+pos,8);std::memcpy(&n,batch.data()+pos+8,4);pos+=12;
                if(n>batch.size()-pos||width>batch.size()-pos-n)throw std::runtime_error("incomplete inserted vector");if(seen&&id<=previous)throw std::runtime_error("unsorted inserted keys");
                consume(id,n,batch.data()+pos,batch.data()+pos+n);previous=id;pos+=n+width;++seen;
            }if(seen!=count)throw std::runtime_error("inserted row count mismatch");};
        each([&](int64_t id,uint32_t n,const unsigned char*,const unsigned char*){scalar_bytes+=4+uint64_t(n);if(!scalar_bytes)throw std::runtime_error("scalar length overflow");if(!extent.keys)extent.minimum=id;extent.maximum=id;extent.keys=1;});
        extent.keys=8+scalar_bytes;extent.row_bytes=extent.keys+8+16*extent.count;extent.vector_bytes=8+extent.count*(8+width);
        Json metadata=Json::object();metadata["kind"]="data";metadata["row_bytes"]=size_t(extent.row_bytes);metadata["vector_bytes"]=size_t(extent.vector_bytes);std::string meta=metadata.dump();extent.rows=uint64_t(target_stat.st_size)+12+meta.size();extent.vectors=extent.rows+extent.row_bytes;
        Frame frame(target,extent.row_bytes+extent.vector_bytes,meta);frame.put("MVROW001",8);
        each([&](int64_t,uint32_t n,const unsigned char *scalar,const unsigned char*){frame.integer(n);frame.put(scalar,n);});
        frame.put("MVKEY002",8);uint64_t row=8;each([&](int64_t id,uint32_t n,const unsigned char*,const unsigned char*){frame.integer(id);frame.integer(row);row+=4+uint64_t(n);});
        frame.put("MVFLT001",8);each([&](int64_t id,uint32_t,const unsigned char*,const unsigned char *vector){frame.integer(id);frame.put(vector,width);});frame.finish();return extent;
    }
    bool needs_checkpoint(const LogCatalog &tables)const{
        uint64_t live=0;for(auto &[_,t]:tables){auto bytes=t.extents.weight();if(bytes>UINT64_MAX-live)return false;live+=bytes;}
        return live<=UINT64_MAX/2&&size()>std::max(uint64_t(1u<<20),2*live);
    }
    void checkpoint(LogCatalog &tables){
        auto next_path=directory/"queryable.next";Descriptor next(::open(next_path.c_str(),O_RDWR|O_CREAT|O_TRUNC,0666));if(next.fd<0)::error("open checkpoint");
        LogCatalog relocated=tables;unsigned char buffer[65536];uint64_t offset=0;
        for(auto &[_,table]:relocated){::PagedDirectory<LogExtent> compacted;
        for(auto e:table.extents){
            if(!e.excluded.empty()){
                Bytes batch;size_t count=0;auto flush=[&](){if(count){compacted.push_back(append(batch,table.schema.dimension(),count,next.fd));batch.clear();count=0;}};
                for(size_t i=0;i<e.count;i++)if(e.visible(i)){
                    int64_t id;uint64_t row;read_at(fd.fd,&id,8,e.rows+e.keys+8+i*16);read_at(fd.fd,&row,8,e.rows+e.keys+16+i*16);
                    uint32_t n;read_at(fd.fd,&n,4,e.rows+row);size_t width=table.schema.dimension()*4;
                    if(!batch.empty()&&batch.size()+12+uint64_t(n)+width>(256u<<10))flush();
                    ::put(batch,id);::put(batch,n);size_t pos=batch.size();batch.resize(pos+uint64_t(n)+width);read_at(fd.fd,batch.data()+pos,n,e.rows+row+4);
                    if(width)read_at(fd.fd,batch.data()+pos+n,width,e.vectors+16+i*(8+width));++count;
                }flush();struct stat current;if(::fstat(next.fd,&current))::error("stat compacted checkpoint");offset=current.st_size;continue;
            }
            Json meta=Json::object();meta["kind"]="data";meta["row_bytes"]=size_t(e.row_bytes);meta["vector_bytes"]=size_t(e.vector_bytes);auto text=meta.dump();
            Frame frame(next.fd,e.row_bytes+e.vector_bytes,text);
            auto copy=[&](uint64_t start,uint64_t length){while(length){size_t n=std::min(length,uint64_t(sizeof(buffer)));read_at(fd.fd,buffer,n,start);frame.put(buffer,n);start+=n;length-=n;}};
            copy(e.rows,e.row_bytes);copy(e.vectors,e.vector_bytes);frame.finish();e.rows=offset+12+text.size();e.vectors=e.rows+e.row_bytes;offset+=24+text.size()+e.row_bytes+e.vector_bytes;compacted.push_back(e);
        }table.extents=std::move(compacted);}
        Json meta=Json::object();meta["kind"]="catalog";meta["tables"]=log_catalog_json(relocated);auto text=meta.dump();Frame commit(next.fd,0,text);commit.finish();offset+=24+text.size();
        if(::fsync(next.fd))::error("sync checkpoint");if(::rename(next_path.c_str(),path.c_str()))::error("install checkpoint");
        Descriptor dir(::open(directory.c_str(),O_RDONLY|O_DIRECTORY));if(dir.fd<0||::fsync(dir.fd))::error("sync checkpoint directory");
        ::close(fd.fd);fd.fd=next.release();if(::fcntl(fd.fd,F_SETFL,O_APPEND))::error("checkpoint append mode");confirmed=offset;tables=std::move(relocated);
    }
    void commit(const LogCatalog &tables,const LogCatalog &previous){Json metadata=Json::object();metadata["kind"]="delta";metadata["changes"]=log_catalog_delta(tables,previous);Frame frame(fd.fd,0,metadata.dump());frame.finish();if(::fsync(fd.fd))::error("sync queryable commit");confirmed=size();}
    void truncate(uint64_t position){if(position<confirmed||position>size())throw std::runtime_error("invalid statement savepoint");if(::ftruncate(fd.fd,position))::error("truncate statement tail");}
    void rollback(){truncate(confirmed);}
};
struct CompiledProjection {std::string columns_json;std::vector<unsigned char> fields;};
struct LogResponse {
    Json status=Json::object();bool query=false;std::vector<std::string> columns,rows;size_t memory=0;std::filesystem::path spill;std::unique_ptr<::File> writer;
    std::shared_ptr<const CompiledProjection> compiled;std::vector<std::pair<double,int64_t>> values;std::array<int64_t,8> constants{};
    bool adopt_compiled(const std::shared_ptr<const CompiledProjection> &projection,std::vector<std::pair<double,int64_t>> &ranked){
        if(!query||!status.is_object()||!status.fields().empty()||!rows.empty()||writer||!spill.empty()||projection->fields.empty()||projection->fields.size()>8)return false;
        size_t reserve=std::min(size_t(16896),projection->columns_json.size()+128+ranked.size()*(3+projection->fields.size()*32));
        if(ranked.capacity()>(65536-reserve)/sizeof(std::pair<double,int64_t>))return false;
        compiled=projection;values=std::move(ranked);memory=values.capacity()*sizeof(std::pair<double,int64_t>);return true;
    }
    void emit_compiled(std::ostream &stream,const Json &id)const{
        std::string out;out.reserve(std::min(size_t(16896),compiled->columns_json.size()+128+values.size()*(3+compiled->fields.size()*32)));
        auto flush=[&](){if(!out.empty()){stream<<out;out.clear();}};
        out+="{\"columns\":";out+=compiled->columns_json;out+=",\"id\":";id.append_to(out);out+=",\"ok\":true,\"rows\":[";if(out.size()>=16384)flush();
        bool first=true;for(auto &[score,key]:values){if(!first)out+=',';first=false;out+='[';
            for(size_t i=0;i<compiled->fields.size();i++){if(i)out+=',';
                if(compiled->fields[i]==1){char bytes[64];auto text=real_chars(std::sqrt(score),bytes);out.append(text.data(),text.size());}
                else{char bytes[32];auto encoded=std::to_chars(bytes,bytes+sizeof(bytes),compiled->fields[i]==0?key:constants.at(compiled->fields[i]-2));if(encoded.ec!=std::errc())throw std::runtime_error("integer formatting failed");out.append(bytes,size_t(encoded.ptr-bytes));}
            }out+=']';if(out.size()>=16384)flush();
        }out+="]}\n";flush();
    }
    ~LogResponse(){writer.reset();if(!spill.empty()){std::error_code ec;std::filesystem::remove(spill,ec);}}
    void append(std::string value,const std::filesystem::path &root){
        memory+=sizeof(std::string)+value.size();if(!writer&&memory>65536){static uint64_t sequence=0;spill=root/("query-result-"+std::to_string(++sequence));writer=std::make_unique<::File>(spill.c_str(),"wbx");for(auto &row:rows){writer->write(row.data(),row.size());writer->write("\n",1);}rows.clear();}
        if(writer){writer->write(value.data(),value.size());writer->write("\n",1);}else rows.push_back(std::move(value));
    }
    void finish(){if(writer){writer->close();writer.reset();}}
};
class LogDatabase {
    QueryableLog log;LogCatalog tables,committed;bool transaction=false;void *mapping=nullptr;bool large_mapping_refused=false;size_t mapped_bytes=0;uint64_t resident_budget=0;
    std::unique_ptr<::RangeDirectory> ranges;const LogTable *range_owner=nullptr;int range_order=0;
    LogIntColumn integer_column;
    struct BlockVectors {
        const LogTable *owner=nullptr;Bytes data;std::vector<uint64_t> records;std::vector<std::array<double,2>> bounds;bool heterogeneous=false;std::array<uint32_t,256> starts{},counts{};std::array<int64_t,256> dictionary{};
        size_t uses=0,column=SIZE_MAX,preferred=SIZE_MAX,groups=0;bool rejected=false,ready=false,regrouped=false;double norm=0,error=0;
        void clear(){owner=nullptr;Bytes().swap(data);std::vector<uint64_t>().swap(records);decltype(bounds)().swap(bounds);heterogeneous=false;uses=groups=0;column=preferred=SIZE_MAX;rejected=ready=regrouped=false;norm=error=0;}
    } block_vectors;
    bool tile_bounds=!std::getenv("MINIDB_TILE_BOUNDS")||std::string(std::getenv("MINIDB_TILE_BOUNDS"))!="0";
    const LogTable *uniform_owner=nullptr;bool uniform_checked=false,uniform_value=false;uint64_t uniform_point=0,uniform_scans=0;
    bool uniform_enabled=!std::getenv("MINIDB_UNIFORM_SCAN")||std::string(std::getenv("MINIDB_UNIFORM_SCAN"))!="0";
    bool compact_budget=!std::getenv("MINIDB_COMPACT_BUDGET")||std::string(std::getenv("MINIDB_COMPACT_BUDGET"))!="0";
    bool large_maps=!std::getenv("MINIDB_LARGE_MAP")||std::string(std::getenv("MINIDB_LARGE_MAP"))!="0";
    bool blocks_enabled=!std::getenv("MINIDB_BLOCK_SCAN")||std::string(std::getenv("MINIDB_BLOCK_SCAN"))!="0";
    bool certify_enabled=!std::getenv("MINIDB_CERTIFY")||std::string(std::getenv("MINIDB_CERTIFY"))!="0";
    uint64_t block_scans=0,block_builds=0,block_refined=0,block_peak=0,block_slots=0,block_groups=0,block_certified=0,block_precise_candidates=0;
    bool quant_enabled=!std::getenv("MINIDB_QUANT_SCREEN")||std::string(std::getenv("MINIDB_QUANT_SCREEN"))!="0";
    uint64_t quant_builds=0,quant_hits=0,quant_peak=0,quant_budget_rejects=0,quant_numeric_rejects=0,map_opens=0;
    uint64_t posting_scans=0,buffered_scans=0,integer_hits=0;
    Bytes quant_cache;std::string quant_name;size_t quant_uses=0;bool quant_rejected=false;double quant_norm=0,quant_error=0;
    const unsigned char *quant_vectors(const std::string &name,const LogTable &table,size_t count,const unsigned char *view){
        if(!quant_enabled||!view||table.schema.dimension()!=128||count<1024||!mv_quant_available())return nullptr;
        if(resident_budget<(64u<<20)){++quant_budget_rejects;return nullptr;}
        if(name!=quant_name){Bytes().swap(quant_cache);quant_name=name;quant_uses=0;quant_rejected=false;}
        if(quant_rejected)return nullptr;if(!quant_cache.empty()){++quant_hits;return quant_cache.data();}
        if(++quant_uses<2)return nullptr;size_t budget=std::min(uint64_t(4u<<20),resident_budget/32);
        size_t occupied=block_vectors.data.capacity()+block_vectors.records.capacity()*8+block_vectors.bounds.capacity()*16+(block_vectors.ready?sizeof(BlockVectors):0);
        budget=occupied<budget?budget-occupied:0;
        if(count>budget/144){++quant_budget_rejects;quant_rejected=true;return nullptr;}
        try{quant_cache.resize(count*144);}catch(const std::bad_alloc &){Bytes().swap(quant_cache);quant_rejected=true;return nullptr;}if(quant_cache.capacity()>budget){++quant_budget_rejects;Bytes().swap(quant_cache);quant_rejected=true;return nullptr;}
        quant_peak=std::max(quant_peak,uint64_t(quant_cache.capacity()));quant_norm=quant_error=0;size_t offset=0;
        for(auto &e:table.extents){if(!mv_quant_pack(view+e.vectors+8,e.count,quant_cache.data()+offset*144,&quant_norm,&quant_error)){++quant_numeric_rejects;Bytes().swap(quant_cache);quant_rejected=true;return nullptr;}offset+=e.count;}
        ++quant_builds;quant_peak=std::max(quant_peak,uint64_t(quant_cache.capacity()));return quant_cache.data();
    }
    struct Prepared {std::shared_ptr<const CompiledProjection> compiled;bool parameterized=false,scores=false,materialized=false;std::vector<size_t> columns;std::vector<bool> needed;std::vector<std::string> labels;std::vector<unsigned char> projection;std::string prefix,suffix,name,label;std::vector<std::string> pieces;std::vector<BoundCondition> conditions;size_t column,dimension,k,charge;};
    std::list<Prepared> plans;size_t plan_bytes=0;uint64_t plan_hits=0,plan_builds=0,batch_hits=0;
    bool plans_enabled=!std::getenv("MINIDB_QUERY_PLANS")||std::string(std::getenv("MINIDB_QUERY_PLANS"))!="0";
    bool batch_output_enabled=!std::getenv("MINIDB_BATCH_OUTPUT")||std::string(std::getenv("MINIDB_BATCH_OUTPUT"))!="0";
    static std::shared_ptr<const CompiledProjection> compile_projection(const std::vector<std::string> &columns,const std::vector<BoundExpr> &outputs,const std::vector<unsigned char> *fields=nullptr){
        auto projection=std::make_shared<CompiledProjection>();Json labels=Json::array();for(auto &name:columns)labels.push(name);projection->columns_json=labels.dump();if(fields)projection->fields=*fields;else for(auto &output:outputs)projection->fields.push_back(output.distance);return projection;
    }
    bool selective_lookup_enabled=!std::getenv("MINIDB_SELECTIVE_LOOKUP")||std::string(std::getenv("MINIDB_SELECTIVE_LOOKUP"))!="0";
    bool point_keys_enabled=!std::getenv("MINIDB_POINT_KEYS")||std::string(std::getenv("MINIDB_POINT_KEYS"))!="0";
    bool projected_lookup_enabled=!std::getenv("MINIDB_PROJECTED_LOOKUP")||std::string(std::getenv("MINIDB_PROJECTED_LOOKUP"))!="0";
    bool scalar_plans_enabled=!std::getenv("MINIDB_SCALAR_PLANS")||std::string(std::getenv("MINIDB_SCALAR_PLANS"))!="0";
    bool predicate_projection_enabled=!std::getenv("MINIDB_PREDICATE_PROJECTION")||std::string(std::getenv("MINIDB_PREDICATE_PROJECTION"))!="0";
    int projection_field(const Table &schema,const BoundExpr &output,const BoundExpr &order,const std::vector<BoundCondition> &conditions)const{
        if(output.distance)return output.column==order.column&&output.query==order.query?1:-1;
        if(output.column==schema.pk)return 0;
        if(predicate_projection_enabled&&schema.columns[output.column].type=="BIGINT")for(size_t i=0;i<std::min(size_t(8),conditions.size());i++)if(conditions[i].column==output.column&&conditions[i].op=="="&&std::holds_alternative<int64_t>(conditions[i].value))return int(i)+2;
        return -1;
    }
    bool covering_plans_enabled=!std::getenv("MINIDB_COVERING_PLANS")||std::string(std::getenv("MINIDB_COVERING_PLANS"))!="0";
    bool parameter_plans_enabled=!std::getenv("MINIDB_PARAMETER_PLANS")||std::string(std::getenv("MINIDB_PARAMETER_PLANS"))!="0";
    bool prepared_select(std::string_view sql,LogResponse &response){
        if(!plans_enabled)return false;
        for(auto &plan:plans){
            if(sql.size()<plan.suffix.size()+2||sql.substr(sql.size()-plan.suffix.size())!=plan.suffix)continue;
            size_t prefix_size=plan.prefix.size();std::array<int64_t,8> bindings{};
            if(!plan.parameterized){
                if(sql.size()<prefix_size+plan.suffix.size()+2||sql.substr(0,prefix_size)!=plan.prefix)continue;
            }else{
                size_t position=0;bool matched=true;
                for(size_t i=0;i<plan.pieces.size();i++){
                    auto &piece=plan.pieces[i];if(position>sql.size()||sql.substr(position,piece.size())!=piece){matched=false;break;}position+=piece.size();
                    if(i==plan.conditions.size())break;
                    if(position>=sql.size()){matched=false;break;}
                    const char *start=sql.data()+position,*end=sql.data()+sql.size();
                    if(*start=='+'){++start;if(start==end||*start=='-'){matched=false;break;}}
                    auto parsed=std::from_chars(start,end,bindings[i]);
                    if(parsed.ec!=std::errc()||parsed.ptr==start){matched=false;break;}position=size_t(parsed.ptr-sql.data());
                }
                if(!matched||position+plan.suffix.size()+2>sql.size())continue;prefix_size=position;
            }
            auto text=sql.substr(prefix_size,sql.size()-prefix_size-plan.suffix.size());
            std::vector<float> values(plan.dimension);if(mv_vector_parse(text.data(),text.size(),values.size(),values.data()))return false;
            auto table=tables.find(plan.name);if(table==tables.end())return false;
            BoundExpr order;order.column=plan.column;order.distance=true;order.query.assign(values.begin(),values.end());
            response.query=true;if(plan.labels.empty())response.columns.push_back(plan.label);else response.columns=plan.labels;
            std::vector<BoundCondition> dynamic;const auto *predicate=&plan.conditions;
            if(plan.parameterized){dynamic=plan.conditions;for(size_t i=0;i<dynamic.size();i++)dynamic[i].value=bindings[i];predicate=&dynamic;}
            auto ranked=topk(plan.name,table->second,order,plan.k,*predicate,plan.scores);
            for(size_t i=0;i<std::min(size_t(8),predicate->size());i++)if(std::holds_alternative<int64_t>((*predicate)[i].value))response.constants[i]=std::get<int64_t>((*predicate)[i].value);
            if(plan.compiled&&response.adopt_compiled(plan.compiled,ranked)){response.finish();++batch_hits;++plan_hits;return true;}
            const unsigned char *row_view=plan.materialized?mapped(table->second):nullptr;
            for(auto &[score,id]:ranked){
                std::optional<Row> stored;if(plan.materialized){stored=lookup(table->second,id,false,row_view,true,&plan.needed);if(!stored||!matches(*stored,*predicate))throw std::runtime_error("invalid prepared TopK row");}
                if(plan.projection.empty())response.append("["+std::to_string(id)+"]",log.root());
                else{std::string row="[";for(size_t i=0;i<plan.projection.size();i++){if(i)row+=',';row+=plan.projection[i]==255?cell_json((*stored)[plan.columns[i]]):plan.projection[i]==1?real(std::sqrt(score)):std::to_string(plan.projection[i]==0?id:response.constants.at(plan.projection[i]-2));}response.append(row+"]",log.root());}
            }response.finish();++plan_hits;return true;
        }return false;
    }
    void remember_select(std::string_view sql,const Statement &st,const Table &schema,const BoundExpr &order,const std::vector<BoundExpr> &outputs,const std::vector<BoundCondition> &conditions,const LogResponse &response){
        if(!plans_enabled||outputs.empty()||outputs.size()>8||conditions.size()>8||schema.dimension()>4096||!schema.dimension())return;
        bool legacy=outputs.size()==1&&!outputs[0].distance&&outputs[0].column==schema.pk&&st.order[0].expr.distance;
        if(!covering_plans_enabled&&!legacy)return;
        bool materialized=false;std::vector<unsigned char> fields;for(auto &output:outputs){int field=projection_field(schema,output,order,conditions);if(field<0){if(!scalar_plans_enabled||output.distance||schema.columns[output.column].type=="VECTOR")return;field=255;materialized=true;}fields.push_back(field);}
        for(auto &c:conditions)if(!std::holds_alternative<int64_t>(c.value))return;
        const Value *source=st.order[0].expr.distance?&st.order[0].expr.query:nullptr;
        if(!source)for(auto &item:st.projection)if(item.expr.distance){source=&item.expr.query;break;}
        if(!source)return;const auto &value=*source;if(value.kind!=Value::Vector||value.text.empty())return;
        size_t first=sql.find('['),last=sql.find(']');
        if(first==sql.npos||last==sql.npos||last<first||sql.find('[',first+1)!=sql.npos||sql.find(']',last+1)!=sql.npos||sql.substr(first,last-first+1)!=value.text)return;
        if(first+sql.size()-last-1>4096)return;
        Prepared plan;plan.prefix=std::string(sql.substr(0,first));plan.suffix=std::string(sql.substr(last+1));plan.name=fold(st.table);plan.label=response.columns[0];plan.conditions=conditions;plan.column=order.column;plan.dimension=schema.dimension();plan.k=*st.limit;
        if(outputs.size()!=1||fields[0]!=0){plan.labels=response.columns;plan.projection=fields;for(auto &output:outputs)plan.scores=plan.scores||output.distance;}
        if(materialized){plan.materialized=true;plan.needed.assign(schema.columns.size(),false);for(auto &output:outputs){plan.columns.push_back(output.column);if(!output.distance)plan.needed[output.column]=true;}for(auto &condition:conditions)plan.needed[condition.column]=true;}
        plan.charge=sizeof(Prepared)+48+plan.prefix.capacity()+plan.suffix.capacity()+plan.name.capacity()+plan.label.capacity()+plan.conditions.capacity()*sizeof(BoundCondition);for(auto &c:plan.conditions)plan.charge+=c.op.capacity();
        plan.charge+=plan.columns.capacity()*sizeof(size_t)+(plan.needed.capacity()+7)/8;plan.charge+=plan.labels.capacity()*sizeof(std::string)+plan.projection.capacity();for(auto &label:plan.labels)plan.charge+=label.capacity();
        if(parameter_plans_enabled&&!conditions.empty()){
            Lexer lexer(plan.prefix);std::vector<std::pair<size_t,size_t>> numbers;bool usable=true;
            for(auto token=lexer.next();token.kind!=Token::End;token=lexer.next())if(token.kind==Token::Number){
                size_t end=lexer.position();if(end<token.text.size()||numbers.size()>=conditions.size()||plan.prefix.substr(end-token.text.size(),token.text.size())!=token.text||!Value::number(token.text).integral){usable=false;break;}
                if(Value::number(token.text).bigint()!=std::get<int64_t>(conditions[numbers.size()].value)){usable=false;break;}
                numbers.emplace_back(end-token.text.size(),end);
            }
            if(usable&&numbers.size()==conditions.size()){
                size_t last=0,extra=0;for(auto [begin,end]:numbers){plan.pieces.push_back(plan.prefix.substr(last,begin-last));last=end;}plan.pieces.push_back(plan.prefix.substr(last));
                extra=plan.pieces.capacity()*sizeof(std::string);for(auto &piece:plan.pieces)extra+=piece.capacity();
                if(plan.charge+extra<=8192)plan.charge+=extra;else std::vector<std::string>().swap(plan.pieces);
            }
        }
        if(plan.charge>8192)return;
        if(batch_output_enabled&&!plan.materialized){
            auto projection=response.compiled?response.compiled:compile_projection(response.columns,outputs,&fields);
            size_t extra=sizeof(CompiledProjection)+64+projection->columns_json.capacity()+projection->fields.capacity();
            if(extra<=8192-plan.charge){plan.compiled=std::move(projection);plan.charge+=extra;}
        }
        for(auto &old:plans)if(old.suffix==plan.suffix&&(old.prefix==plan.prefix||(old.parameterized&&!plan.pieces.empty()&&old.pieces==plan.pieces)))return;
        if(!plan.pieces.empty()){
            size_t variants=0;for(auto &old:plans)if(old.suffix==plan.suffix&&old.pieces==plan.pieces)++variants;
            if(variants>=5){
                plan.parameterized=true;
                for(auto it=plans.begin();it!=plans.end();)if(it->suffix==plan.suffix&&it->pieces==plan.pieces){plan_bytes-=it->charge;it=plans.erase(it);}else ++it;
            }
        }
        while(!plans.empty()&&(plans.size()>=8||plan_bytes+plan.charge>65536)){plan_bytes-=plans.front().charge;plans.pop_front();}
        plans.push_back(std::move(plan));plan_bytes+=plans.back().charge;++plan_builds;
    }
    struct Mask {std::string key,bytes;size_t charge;bool empty;std::vector<uint32_t> positions;};
    bool postings_enabled=!std::getenv("MINIDB_POSTINGS")||std::string(std::getenv("MINIDB_POSTINGS"))!="0";std::list<Mask> masks;size_t mask_bytes=0;
    void invalidate(){uniform_owner=nullptr;uniform_checked=uniform_value=false;uniform_point=0;block_vectors.clear();plans.clear();plan_bytes=0;Bytes().swap(quant_cache);quant_name.clear();quant_uses=0;quant_rejected=false;integer_column.clear();ranges.reset();range_owner=nullptr;if(mapping)::munmap(mapping,mapped_bytes);mapping=nullptr;large_mapping_refused=false;mapped_bytes=0;masks.clear();mask_bytes=0;}
    void collect(){if(log.needs_checkpoint(tables)){invalidate();log.checkpoint(tables);committed=tables;}}
    void publish(){if(!transaction){log.commit(tables,committed);committed=tables;collect();}}
    const unsigned char *mapped(const LogTable &table){
        if(std::getenv("MINIDB_INDEX_MMAP")&&std::string(std::getenv("MINIDB_INDEX_MMAP"))=="0")return nullptr;
        if(mapping)return static_cast<const unsigned char*>(mapping);if(large_mapping_refused)return nullptr;uint64_t size=log.size(),map_limit=uint64_t(PTRDIFF_MAX);if(resident_budget<=map_limit/32)map_limit=std::max(uint64_t(64u<<20),resident_budget*32);if((!large_maps&&size>(64u<<20))||size>map_limit)return nullptr;uint64_t left=resident_budget;
        for(auto &e:table.extents){if(e.vector_bytes>left)return nullptr;left-=e.vector_bytes;}
        void *out=::mmap(nullptr,size,PROT_READ,MAP_PRIVATE,log.descriptor(),0);if(out==MAP_FAILED){if(size>(64u<<20))large_mapping_refused=true;return nullptr;}mapping=out;mapped_bytes=size;++map_opens;return static_cast<const unsigned char*>(out);
    }
    void read(void *out,size_t n,uint64_t offset,const unsigned char *view)const{if(view){if(offset>mapped_bytes||n>mapped_bytes-offset)throw std::runtime_error("extent read outside mapping");std::memcpy(out,view+offset,n);}else read_at(log.descriptor(),out,n,offset);}
    bool uniform_topk(const LogTable &table,const unsigned char *view,size_t physical,const BoundExpr &expr,size_t k,const std::vector<BoundCondition> &conditions,::TopK &heap){
        if(!uniform_enabled||!view||physical<1024||expr.query.empty()||expr.query.size()>4096)return false;
        size_t width=expr.query.size()*4,stride=width+8;
        if(uniform_owner!=&table){uniform_owner=&table;uniform_checked=uniform_value=false;uniform_point=0;}
        if(!uniform_checked){
            uniform_checked=true;bool found=false;
            for(auto &e:table.extents)for(size_t i=0;i<e.count;i++)if(e.visible(i)){
                uint64_t point=e.vectors+16+i*stride;
                if(point>mapped_bytes||width>mapped_bytes-point)throw std::runtime_error("uniform vector outside mapping");
                if(!found){uniform_point=point;found=true;}
                else if(std::memcmp(view+uniform_point,view+point,width))return false;
            }
            uniform_value=found;
        }
        if(!uniform_value)return false;
        ::PositiveSum sum;for(size_t j=0;j<expr.query.size();j++){float x;std::memcpy(&x,view+uniform_point+j*4,4);double delta=double(x)-expr.query[j];sum.add(delta*delta);}double score=sum.result();
        LogPredicate predicate(table.schema,conditions);
        for(auto &e:table.extents){
            if(heap.heap.size()==k&&e.minimum>=heap.heap.front().second)continue;
            auto span=scan_interval(e,table.schema,conditions);
            for(size_t i=span.first;i<span.second;i++)if(e.visible(i)){
                int64_t id;read(&id,8,e.rows+e.keys+8+i*16,view);
                if(heap.heap.size()==k&&id>=heap.heap.front().second)break;
                if(!conditions.empty()){
                    uint64_t row;read(&row,8,e.rows+e.keys+16+i*16,view);uint32_t n;read(&n,4,e.rows+row,view);uint64_t offset=e.rows+row+4;
                    if(offset>mapped_bytes||n>mapped_bytes-offset)throw std::runtime_error("uniform scalar outside mapping");
                    if(!predicate.matches(view+offset,n))continue;
                }
                heap.offer_score(score,id);
            }
        }
        ++uniform_scans;return true;
    }
    bool accepts_scalar(const Table &table,const Bytes &scalar,const std::vector<BoundCondition> &conditions){for(auto &c:conditions)if(!accepts(compare_cell(scalar_field(table,scalar,c.column),c.value),c.op))return false;return true;}
    Row selective_row(const Table &schema,uint64_t scalar,size_t size,uint64_t vector,size_t width,const std::vector<bool> &needed){
        std::array<unsigned char,256> prefix;size_t available=std::min(size,prefix.size());read(prefix.data(),available,scalar,nullptr);
        auto field=[&](void *out,size_t bytes,size_t position){if(position>size||bytes>size-position)throw std::runtime_error("incomplete projected field");if(position<=available&&bytes<=available-position)std::memcpy(out,prefix.data()+position,bytes);else read(out,bytes,scalar+position,nullptr);};
        Row row(schema.columns.size());size_t position=0;
        for(size_t i=0;i<schema.columns.size();i++){
            auto &column=schema.columns[i];bool keep=needed.at(i);
            if(column.type=="VECTOR"){if(keep){if(width!=column.n*4)throw std::runtime_error("invalid projected vector");std::vector<float> value(column.n);read(value.data(),width,vector,nullptr);row[i]=std::move(value);}continue;}
            if(column.type=="BIGINT"){if(position>size||size-position<8)throw std::runtime_error("incomplete projected integer");if(keep){int64_t value;field(&value,8,position);row[i]=value;}position+=8;}
            else{uint32_t length;field(&length,4,position);position+=4;if(length>size-position)throw std::runtime_error("incomplete projected text");if(keep){std::string value(length,'\0');field(value.data(),length,position);row[i]=std::move(value);}position+=length;}
        }
        if(position!=size)throw std::runtime_error("extra projected scalar bytes");return row;
    }
    std::optional<Row> lookup(const LogTable &table,int64_t id,bool vectors,const unsigned char *view,bool indexed=true,const std::vector<bool> *needed=nullptr){
        std::optional<Row> result;std::array<unsigned char,4096> key_buffer;
        auto visit=[&](size_t ordinal){auto &e=table.extents[ordinal];if(id<e.minimum||id>e.maximum)return false;const unsigned char *keys=nullptr;if(point_keys_enabled&&!view&&e.count<=key_buffer.size()/16){read(key_buffer.data(),e.count*16,e.rows+e.keys+8,nullptr);keys=key_buffer.data();}size_t lo=0,hi=e.count;bool found=false;
            while(lo<hi){size_t mid=lo+(hi-lo)/2;int64_t key;if(keys)std::memcpy(&key,keys+mid*16,8);else read(&key,8,e.rows+e.keys+8+mid*16,view);if(key<id)lo=mid+1;else if(key>id)hi=mid;else{lo=mid;found=true;break;}}
            if(!found||!e.visible(lo))return false;uint64_t offset;if(keys)std::memcpy(&offset,keys+lo*16+8,8);else read(&offset,8,e.rows+e.keys+8+lo*16+8,view);uint32_t n;read(&n,4,e.rows+offset,view);
            if(!projected_lookup_enabled){Bytes scalar(n),vector(vectors?table.schema.dimension()*4:0);read(scalar.data(),n,e.rows+offset+4,view);if(vectors&&table.schema.vector_column>=0)read(vector.data(),vector.size(),e.vectors+16+lo*(8+table.schema.dimension()*4),view);std::vector<bool> wanted(table.schema.columns.size(),true);if(!vectors&&table.schema.vector_column>=0)wanted[table.schema.vector_column]=false;result=Codec{table.schema}.decode(scalar,vector,&wanted);return true;}
            std::vector<bool> defaults;if(!needed){defaults.assign(table.schema.columns.size(),true);if(!vectors&&table.schema.vector_column>=0)defaults[table.schema.vector_column]=false;}
            const auto *mask=needed?needed:&defaults;size_t width=vectors?table.schema.dimension()*4:0;uint64_t vector_offset=e.vectors+16+lo*(8+table.schema.dimension()*4),scalar_offset=e.rows+offset+4;
            if(!view&&selective_lookup_enabled&&n>=1024){bool skip=false;for(size_t i=0;i<table.schema.columns.size();i++)if(table.schema.columns[i].type=="VARCHAR"&&!mask->at(i)){skip=true;break;}if(skip){result=selective_row(table.schema,scalar_offset,n,vector_offset,width,*mask);return true;}}
            if(view){if(scalar_offset>mapped_bytes||n>mapped_bytes-scalar_offset||(width&&(vector_offset>mapped_bytes||width>mapped_bytes-vector_offset)))throw std::runtime_error("projected row outside mapping");result=Codec{table.schema}.decode_view(view+scalar_offset,n,width?view+vector_offset:nullptr,width,mask);}
            else{Bytes scalar(n),vector(width);read(scalar.data(),n,scalar_offset,nullptr);if(width)read(vector.data(),width,vector_offset,nullptr);result=Codec{table.schema}.decode(scalar,vector,mask);}return true;
        };
        if(indexed&&table.extents.size()>=16){
            if(range_owner!=&table){
                ranges.reset();range_order=1;bool ascending=true,descending=true;
                for(size_t i=1;i<table.extents.size();i++){ascending=ascending&&table.extents[i-1].maximum<table.extents[i].minimum;descending=descending&&table.extents[i].maximum<table.extents[i-1].minimum;}
                range_order=ascending?1:descending?-1:0;
                if(!range_order)ranges=std::make_unique<::RangeDirectory>(log.root(),table.extents);range_owner=&table;
            }
            if(range_order){
                size_t lo=0,hi=table.extents.size();auto position=[&](size_t i){return range_order>0?i:table.extents.size()-1-i;};
                while(lo<hi){size_t mid=lo+(hi-lo)/2;if(table.extents[position(mid)].minimum<=id)lo=mid+1;else hi=mid;}
                if(lo)visit(position(lo-1));
            }else ranges->find(id,visit);
        }
        else for(size_t i=0;i<table.extents.size();i++)if(visit(i))break;
        return result;
    }
    std::pair<size_t,size_t> scan_interval(const LogExtent &e,const Table &schema,const std::vector<BoundCondition> &conditions){
        int64_t lower=INT64_MIN,upper=INT64_MAX;bool constrained=false;
        for(auto &c:conditions)if(c.column==schema.pk){
            constrained=true;auto value=std::get<int64_t>(c.value);
            if(c.op=="="){lower=std::max(lower,value);upper=std::min(upper,value);}
            else if(c.op==">"){if(value==INT64_MAX)return {0,0};lower=std::max(lower,int64_t(value+1));}
            else if(c.op==">=")lower=std::max(lower,value);
            else if(c.op=="<"){if(value==INT64_MIN)return {0,0};upper=std::min(upper,int64_t(value-1));}
            else if(c.op=="<=")upper=std::min(upper,value);
        }
        if(lower>upper||upper<e.minimum||lower>e.maximum)return {0,0};if(!constrained)return {0,e.count};
        auto bound=[&](int64_t value,bool inclusive){size_t lo=0,hi=e.count;while(lo<hi){size_t mid=lo+(hi-lo)/2;int64_t key;read_at(log.descriptor(),&key,8,e.rows+e.keys+8+mid*16);if(key<value||(inclusive&&key==value))lo=mid+1;else hi=mid;}return lo;};
        return {lower<=e.minimum?0:bound(lower,false),upper>=e.maximum?e.count:bound(upper,true)};
    }
    Row extent_row(const LogTable &table,const LogExtent &e,size_t index,const std::vector<bool> *needed=nullptr){
        uint64_t offset;read_at(log.descriptor(),&offset,8,e.rows+e.keys+16+index*16);
        uint32_t n;read_at(log.descriptor(),&n,4,e.rows+offset);bool vectors=table.schema.vector_column>=0&&(!needed||(*needed)[table.schema.vector_column]);Bytes scalar(n),vector(vectors?table.schema.dimension()*4:0);
        read_at(log.descriptor(),scalar.data(),n,e.rows+offset+4);
        if(vectors)read_at(log.descriptor(),vector.data(),vector.size(),e.vectors+16+index*(8+table.schema.dimension()*4));
        return Codec{table.schema}.decode(scalar,vector,needed);
    }
    const LogIntColumn *cached_integer_column(const LogTable &table,size_t column,size_t physical,const unsigned char *view){
        try{
        if(!integer_column.request(table,column))return nullptr;if(integer_column.ready)return &integer_column;
        integer_column.attempted=true;size_t budget=std::min(size_t(128u<<10),size_t(resident_budget/128));
        if(budget<sizeof(LogIntColumn)||physical>budget-sizeof(LogIntColumn))return nullptr;
        auto &cache=integer_column;cache.codes.reserve(physical);if(cache.codes.capacity()>budget-sizeof(LogIntColumn)){Bytes().swap(cache.codes);return nullptr;}
        for(auto &e:table.extents){uint64_t row=e.rows+8;LogReadWindow window(log.descriptor(),e.rows+e.keys);
            for(size_t ordinal=0;ordinal<e.count;ordinal++){
                uint32_t n;if(view)read(&n,4,row,view);else std::memcpy(&n,window.get(row,4),4);
                if(row>e.rows+e.keys||e.rows+e.keys-row<4||n>e.rows+e.keys-row-4)throw std::runtime_error("column row outside extent");
                size_t pos=0;int64_t value=0;
                auto field=[&](void *out,size_t length){if(pos>n||length>n-pos)throw std::runtime_error("incomplete column field");if(view)read(out,length,row+4+pos,view);else std::memcpy(out,window.get(row+4+pos,length),length);pos+=length;};
                for(size_t col=0;col<=column;col++){
                    auto &type=table.schema.columns[col].type;if(type=="VECTOR")continue;
                    if(type=="BIGINT"){if(col==column)field(&value,8);else{if(pos>n||n-pos<8)throw std::runtime_error("incomplete integer prefix");pos+=8;}}
                    else{uint32_t length;field(&length,4);if(length>n-pos)throw std::runtime_error("incomplete text prefix");pos+=length;}
                }
                if(!cache.append(value)){Bytes().swap(cache.codes);return nullptr;}row+=4+uint64_t(n);
            }
        }
        cache.ready=true;return &cache;
        }catch(const std::bad_alloc&){integer_column.clear();return nullptr;}
    }
    uint64_t block_cache_budget()const{
        if(!compact_budget)return std::min(uint64_t(4u<<20),resident_budget/32);
        uint64_t budget=std::min(uint64_t(4u<<20),resident_budget/8),anon=0;bool found=false;
        std::ifstream status("/proc/self/status");std::string line;
        while(std::getline(status,line))if(line.rfind("RssAnon:",0)==0){std::istringstream input(line.substr(8));uint64_t kb;std::string unit;if(input>>kb>>unit&&unit=="kB"&&kb<=UINT64_MAX/1024){anon=kb*1024;found=true;}break;}
        if(!found)return resident_budget<(64u<<20)?0:std::min(uint64_t(4u<<20),resident_budget/32);
        return std::min(budget,anon<resident_budget?resident_budget-anon:0);
    }
    bool blocked_topk(const LogTable &table,const unsigned char *view,size_t physical,const BoundExpr &expr,size_t k,const std::vector<BoundCondition> &conditions,::TopK &heap,bool require_scores){
        if(!blocks_enabled||postings_enabled||!quant_enabled||!view||expr.query.size()!=128||physical<1024||resident_budget<(compact_budget?(8u<<20):(64u<<20))||!mv_quant_available())return false;
        auto &cache=block_vectors;if(cache.owner!=&table){cache.clear();cache.owner=&table;}if(cache.rejected)return false;
        // A first scan may precede any filter. Allow one transition from an
        // ungrouped layout to the first observed compatible filter column.
        if(cache.ready&&cache.column==SIZE_MAX&&!cache.regrouped&&!conditions.empty()){
            size_t candidate=conditions.front().column;bool compatible=candidate!=table.schema.pk&&table.schema.columns[candidate].type=="BIGINT";
            for(auto &condition:conditions)compatible=compatible&&condition.column==candidate;
            if(compatible&&cache.preferred!=candidate){cache.clear();cache.owner=&table;cache.preferred=candidate;cache.regrouped=true;}
        }
        if(!cache.ready){
            if(!conditions.empty()){
                size_t candidate=conditions.front().column;bool compatible=candidate!=table.schema.pk&&table.schema.columns[candidate].type=="BIGINT";
                for(auto &condition:conditions)compatible=compatible&&condition.column==candidate;
                if(compatible)cache.preferred=candidate;
            }
            ++cache.uses;
            const LogIntColumn *codes=nullptr;size_t col=cache.preferred;
            // Bound preparation when alternating predicates keep replacing
            // the single-column dictionary before it can become ready.
            if(cache.uses>4){col=SIZE_MAX;cache.regrouped=true;}
            else{
                if(col==SIZE_MAX)for(size_t i=0;i<table.schema.columns.size();i++)if(i!=table.schema.pk&&table.schema.columns[i].type=="BIGINT"){col=i;break;}
                if(col!=SIZE_MAX)codes=cached_integer_column(table,col,physical,view);
            }
            if(cache.uses<2||(col!=SIZE_MAX&&!codes&&!integer_column.attempted))return false;
            if(codes)++integer_hits;
            size_t groups=codes?codes->distinct:1;std::array<size_t,256> counts{},cursor{};size_t ordinal=0;
            for(auto &e:table.extents){for(size_t i=0;i<e.count;i++)if(e.visible(i))++counts[codes?codes->codes[ordinal+i]:0];ordinal+=e.count;}
            size_t slots=0;for(size_t g=0;g<groups;g++){cache.starts[g]=slots;cache.counts[g]=counts[g];cursor[g]=slots;slots+=(counts[g]+15)/16*16;}
            size_t budget=block_cache_budget();
            if(budget<=sizeof(BlockVectors)||!slots||slots>(budget-sizeof(BlockVectors))/(tile_bounds?153:152)){cache.rejected=true;return false;}
            try{
                // Formats share one budget. Release the previous row cache
                // before allocating its blocked replacement.
                Bytes().swap(quant_cache);quant_name.clear();quant_uses=0;quant_rejected=false;
                cache.data.resize(slots*144);cache.records.resize(slots);if(tile_bounds)cache.bounds.resize(slots/16);
                if(cache.data.capacity()+cache.records.capacity()*8+cache.bounds.capacity()*16+sizeof(BlockVectors)>budget){cache.clear();cache.owner=&table;cache.rejected=true;return false;}
                ordinal=0;for(auto &e:table.extents){for(size_t i=0;i<e.count;i++)if(e.visible(i)){
                    size_t g=codes?codes->codes[ordinal+i]:0,slot=cursor[g]++;uint64_t offset=e.vectors+8+i*520;
                    cache.records[slot]=offset;double &norm=tile_bounds?cache.bounds[slot/16][0]:cache.norm,&error=tile_bounds?cache.bounds[slot/16][1]:cache.error;if(!mv_quant_pack_block_row(view+offset,cache.data.data(),slot,&norm,&error)){cache.clear();cache.owner=&table;cache.rejected=true;return false;}
                }ordinal+=e.count;}
                if(tile_bounds){double minimum=INFINITY;for(auto &bound:cache.bounds){cache.norm=std::max(cache.norm,bound[0]);cache.error=std::max(cache.error,bound[1]);if(bound[0]>0)minimum=std::min(minimum,bound[0]);}cache.heterogeneous=cache.norm>4*minimum;}
                cache.groups=groups;cache.preferred=col;if(codes)cache.dictionary=codes->dictionary;cache.column=codes?col:SIZE_MAX;cache.ready=true;++block_builds;block_peak=std::max(block_peak,uint64_t(cache.data.capacity()+cache.records.capacity()*8+cache.bounds.capacity()*16+sizeof(BlockVectors)));block_slots=slots;block_groups=groups;
            }catch(const std::bad_alloc&){cache.clear();cache.owner=&table;cache.rejected=true;return false;}
        }
        for(auto &c:conditions)if(c.column!=cache.column||!std::holds_alternative<int64_t>(c.value))return false;
        std::array<uint32_t,512> ranges{};size_t n=0;
        for(size_t g=0;g<cache.groups;g++)if(cache.counts[g]){
            bool accept=true;for(auto &c:conditions){auto actual=cache.dictionary[g],value=std::get<int64_t>(c.value);accept=accept&&accepts(actual<value?-1:actual>value?1:0,c.op);}
            if(accept){ranges[n++]=cache.starts[g];ranges[n++]=cache.counts[g];}
        }
        if(!n)return true;
        size_t active=0;for(size_t i=1;i<n;i+=2)active+=ranges[i];
        const char *tiny_flag=std::getenv("MINIDB_TINY_EXACT");
        if(certify_enabled&&!require_scores&&active<=64&&(!tiny_flag||std::strcmp(tiny_flag,"0"))){
            heap.positions.clear();for(size_t r=0;r<n;r+=2)for(size_t i=0;i<ranges[r+1];i++)heap.positions.push_back(ranges[r]+i);
            std::array<double,1024> scores;int certified=0;
            size_t found=mv_refine_block_candidates(view,cache.records.data(),heap.query.data(),k,heap.positions.data(),active,scores.data(),&certified);
            if(found!=SIZE_MAX&&certified){for(size_t i=0;i<found;i++){int64_t id;std::memcpy(&id,view+cache.records[heap.positions[i]],8);heap.offer_score(scores[i],id);}++block_scans;++block_certified;block_precise_candidates+=active;return true;}
        }
        bool prepared=mv_quant_prepare(heap.query.data(),cache.norm,cache.error,view+cache.records[0]+8),granular=tile_bounds&&cache.heterogeneous;if(!prepared&&!granular)return false;
        heap.positions.resize(cache.records.size());size_t found=granular?mv_quant_topk_bound_blocks(cache.data.data(),cache.records.size(),cache.bounds[0].data(),ranges.data(),n/2,k,heap.positions.data()):mv_quant_topk_blocks(cache.data.data(),cache.records.size(),ranges.data(),n/2,k,heap.positions.data());
        if(found==SIZE_MAX||found>cache.records.size())throw std::runtime_error("invalid blocked query result");
        block_precise_candidates+=found;
        if(certify_enabled&&found&&found<=std::max(size_t(256),4*k)&&(!require_scores||found>k)){
            std::array<double,1024> scores;int certified=0;
            size_t narrowed=mv_refine_block_candidates(view,cache.records.data(),heap.query.data(),k,heap.positions.data(),found,scores.data(),&certified);
            if(narrowed!=SIZE_MAX){
                found=narrowed;
                if(certified&&!require_scores){
                    for(size_t i=0;i<found;i++){int64_t id;std::memcpy(&id,view+cache.records[heap.positions[i]],8);heap.offer_score(scores[i],id);}
                    ++block_scans;++block_certified;return true;
                }
            }
        }
        for(size_t i=0;i<found;i++)heap.offer(view+cache.records[heap.positions[i]]);
        ++block_scans;block_refined+=found;return true;
    }
    size_t mutate(LogTable &table,const Statement &st){
        auto predicate=conditions(table.schema,st);std::vector<std::pair<size_t,Cell>> assignments;
        for(auto &[name,value]:st.sets){size_t col=table.schema.column(name);if(col==table.schema.pk)fail("primary key update unsupported");
            for(auto &a:assignments)if(a.first==col)fail("duplicate assignment");assignments.emplace_back(col,convert(table.schema.columns[col],value));}
        // Append only selected replacement rows. The transaction directory owns
        // ordinal visibility; old payload stays immutable until checkpoint.
        invalidate();uint64_t savepoint=log.size();LogTable next=table;size_t affected=0;
        try{
            for(size_t part=0;part<table.extents.size();part++){
                const auto &extent=table.extents[part];std::vector<uint64_t> hidden=extent.excluded;Bytes batch;size_t count=0;
                auto flush=[&](){if(count){next.extents.push_back(log.append(batch,table.schema.dimension(),count));batch.clear();count=0;}};
                auto interval=scan_interval(extent,table.schema,predicate);std::vector<bool> wanted(table.schema.columns.size(),st.op=="update");
                if(st.op=="delete"){wanted[table.schema.pk]=true;for(auto &c:predicate)wanted[c.column]=true;}
                LogBlockScanner scan(log.descriptor(),table.schema,extent,wanted,interval.first,interval.second,&predicate);Row row;size_t i;
                while(scan.next(row,i)){
                    if(!matches(row,predicate))continue;
                    ++affected;hidden.push_back(i);if(st.op=="delete"){--next.schema.count;continue;}
                    for(auto &[col,value]:assignments)row[col]=value;
                    auto scalar=Codec{table.schema}.scalars(row);if(scalar.size()>UINT32_MAX)fail("row too large","invalid_value");
                    size_t width=table.schema.dimension()*4;if(!batch.empty()&&batch.size()+scalar.size()+width+12>(256u<<10))flush();
                    ::put(batch,std::get<int64_t>(row[table.schema.pk]));::put(batch,uint32_t(scalar.size()));batch.insert(batch.end(),scalar.begin(),scalar.end());
                    if(table.schema.vector_column>=0){auto &v=std::get<std::vector<float>>(row[table.schema.vector_column]);auto *p=reinterpret_cast<const unsigned char*>(v.data());batch.insert(batch.end(),p,p+v.size()*4);}++count;
                }flush();std::sort(hidden.begin(),hidden.end());if(hidden!=extent.excluded)next.extents.edit(part).excluded=std::move(hidden);
            }
            bool dead=false;for(auto &e:next.extents)dead=dead||e.excluded.size()==e.count;
            if(dead){::PagedDirectory<LogExtent> live;for(auto &e:next.extents)if(e.excluded.size()!=e.count)live.push_back(e);next.extents=std::move(live);}
        }catch(...){log.truncate(savepoint);throw;}
        if(affected){table=std::move(next);publish();}return affected;
    }
    std::string project(const Row &row,const std::vector<BoundExpr> &outputs,const BoundExpr *known=nullptr,double score=0){
        std::string out="[";for(size_t i=0;i<outputs.size();i++){if(i)out+=',';auto &e=outputs[i];if(e.distance){double d=known&&e.column==known->column&&e.query==known->query?score:squared_distance(std::get<std::vector<float>>(row[e.column]),e.query);out+=real(std::sqrt(d));}else out+=cell_json(row[e.column]);}return out+"]";
    }
    BoundExpr bind(const Table &table,const Expr &e){BoundExpr out;out.column=table.column(e.name);out.distance=e.distance;if(e.distance){if(table.columns[out.column].type!="VECTOR")fail("distance requires vector","invalid_vector");auto v=vector_value(e.query,table.columns[out.column].n);out.query.assign(v.begin(),v.end());}return out;}
    std::vector<BoundCondition> conditions(const Table &table,const Statement &st){std::vector<BoundCondition> out;for(auto &c:st.conditions){size_t col=table.column(c.column);out.push_back({col,c.op,convert(table.columns[col],c.value,true)});}return out;}
    static void unpack(const std::string &bits,size_t first,size_t count,unsigned char *out){
        static constexpr auto table=[]() constexpr{std::array<std::array<unsigned char,8>,256> value{};for(size_t i=0;i<256;i++)for(size_t j=0;j<8;j++)value[i][j]=(i>>j)&1;return value;}();
        const auto *input=reinterpret_cast<const unsigned char*>(bits.data());size_t done=0;
        for(;done+8<=count;done+=8){size_t pos=first+done,index=pos/8,shift=pos%8;unsigned byte=input[index]>>shift;if(shift)byte|=unsigned(input[index+1])<<(8-shift);std::memcpy(out+done,table[byte&255].data(),8);}
        for(;done<count;done++){size_t pos=first+done;out[done]=(input[pos/8]>>(pos%8))&1;}
    }
    std::vector<std::pair<double,int64_t>> topk(const std::string &name,const LogTable &table,const BoundExpr &expr,size_t k,const std::vector<BoundCondition> &conditions,bool require_scores=true){
        // Narrow key ranges use the same bounded scanner as SQL and DML.
        // Count physical candidates (including hidden versions) before choosing
        // this route; a sparse logical count must not admit an unbounded scan.
        if(std::any_of(conditions.begin(),conditions.end(),[&](auto &c){return c.column==table.schema.pk;})){
            size_t limit=std::min(size_t(4096),std::max(size_t(1),table.schema.count/64)),candidates=0;
            for(auto &e:table.extents){auto span=scan_interval(e,table.schema,conditions);auto n=span.second-span.first;if(n>limit-candidates){candidates=limit+1;break;}candidates+=n;}
            if(candidates<=limit){
                ::TopK exact(expr.query.data(),expr.query.size(),k);std::vector<bool> needed(table.schema.columns.size(),false);needed[expr.column]=true;needed[table.schema.pk]=true;for(auto &c:conditions)needed[c.column]=true;
                for(auto &e:table.extents){auto span=scan_interval(e,table.schema,conditions);LogBlockScanner scan(log.descriptor(),table.schema,e,needed,span.first,span.second,&conditions);Row row;size_t ordinal;
                    while(scan.next(row,ordinal))if(matches(row,conditions))exact.offer_score(squared_distance(std::get<std::vector<float>>(row[expr.column]),expr.query),std::get<int64_t>(row[table.schema.pk]));}
                std::sort(exact.heap.begin(),exact.heap.end());return exact.heap;
            }
        }
        size_t physical=0;bool filtering=!conditions.empty();for(auto &e:table.extents){physical+=e.count;filtering=filtering||!e.excluded.empty();}
        ::TopK heap(expr.query.data(),expr.query.size(),k);
        const unsigned char *vector_view=mapped(table);
        if(uniform_topk(table,vector_view,physical,expr,k,conditions,heap)||blocked_topk(table,vector_view,physical,expr,k,conditions,heap,require_scores)){std::sort(heap.heap.begin(),heap.heap.end());return heap.heap;}
        size_t stride=8+expr.query.size()*4;Bytes data;LogPredicate predicate(table.schema,conditions);
        std::string key=quote(name);for(auto &c:conditions)key+='|'+std::to_string(c.column)+c.op+cell_json(c.value);
        const std::string *cached=nullptr;const std::vector<uint32_t> *postings=nullptr;size_t posting_cursor=0;
        if(filtering)for(auto it=masks.begin();it!=masks.end();++it)if(it->key==key){masks.splice(masks.end(),masks,it);if(masks.back().empty)return {};cached=&masks.back().bytes;if(!masks.back().positions.empty())postings=&masks.back().positions;break;}
        size_t cache_bytes=physical/8+(physical%8!=0);if(cached&&cached->size()!=cache_bytes)throw std::runtime_error("invalid query predicate bitmap");
        size_t mask_limit=std::min(size_t(128u<<10),size_t(resident_budget/128));
        std::string pending;bool build=filtering&&!cached&&cache_bytes+key.size()+sizeof(Mask)+48<=mask_limit;
        if(build)try{pending.assign(cache_bytes,char(0));}catch(const std::bad_alloc&){build=false;}
        const auto *view=mapped(table);if(!view)postings=nullptr;const LogIntColumn *column=nullptr;std::array<unsigned char,256> selected{};
        if(!cached&&!conditions.empty()){
            size_t col=conditions.front().column;bool eligible=table.schema.columns[col].type=="BIGINT";
            for(auto &condition:conditions)eligible=eligible&&condition.column==col;
            if(eligible)column=cached_integer_column(table,col,physical,view);
            if(column)++integer_hits;
            if(column)for(size_t code=0;code<column->distinct;code++){
                bool accept=true;for(auto &condition:conditions){auto value=std::get<int64_t>(condition.value),actual=column->dictionary[code];accept=accept&&accepts(actual<value?-1:actual>value?1:0,condition.op);}selected[code]=accept;
            }
        }
        const unsigned char *quant=quant_vectors(name,table,physical,view);
        size_t target=view&&resident_budget>=(28u<<20)?(4u<<20):(256u<<10),block=std::max(size_t(1),target/stride);
        Bytes expanded;if(filtering)expanded.resize(block);std::vector<mv_record_span> spans;spans.reserve(std::min(size_t(1024),table.extents.size()));size_t queued=0;
        auto flush=[&](){
            if(!queued)return;heap.positions.resize(queued);size_t found=mv_screen_topk_spans(spans.data(),spans.size(),expr.query.size(),heap.query.data(),heap.bound(),k,heap.positions.data());
            if(found==SIZE_MAX||found>queued)throw std::runtime_error("logical log scan failed");size_t span=0,base=0;
            for(size_t i=0;i<found;i++){size_t pos=heap.positions[i];while(span<spans.size()&&pos>=base+spans[span].count){base+=spans[span].count;++span;}if(span==spans.size())throw std::runtime_error("invalid logical candidate");heap.offer(spans[span].data+(pos-base)*stride);}
            spans.clear();queued=0;
        };
        struct Plan {~Plan(){mv_screen_end_query();}} plan;
        if(!table.extents.empty()){
            auto &first=table.extents.front();size_t bytes=std::min(block,size_t(first.count))*stride;const unsigned char *sample;
            if(view)sample=view+first.vectors+8;else{data.resize(bytes);read(data.data(),bytes,first.vectors+8,nullptr);sample=data.data();}
            if(!mv_screen_begin_query(sample,bytes,expr.query.size(),heap.query.data(),table.schema.count))throw std::runtime_error("invalid shared precision plan");
        }
        if(quant&&!mv_quant_prepare(heap.query.data(),quant_norm,quant_error,view+table.extents.front().vectors+16))quant=nullptr;
        size_t base_row=0;
        for(auto &e:table.extents){
            uint64_t row=e.rows+8;LogReadWindow scalar_window(log.descriptor(),e.rows+e.keys);
            for(size_t first=0;first<e.count;){size_t count=std::min(block-queued,size_t(e.count-first));const unsigned char *filter=nullptr;const uint32_t *indices=nullptr;size_t index_count=0;
                if(postings){++posting_scans;size_t begin=posting_cursor;while(posting_cursor<postings->size()&&(*postings)[posting_cursor]<base_row+first+count)++posting_cursor;index_count=posting_cursor-begin;if(!index_count){first+=count;continue;}indices=postings->data()+begin;}
                else if(filtering){
                    auto *out=expanded.data()+queued;
                    if(cached)unpack(*cached,base_row+first,count,out);
                    else if(column){for(size_t i=0;i<count;i++){bool visible=e.visible(first+i)&&selected[column->codes[base_row+first+i]];out[i]=visible;if(build&&visible)reinterpret_cast<unsigned char*>(pending.data())[(base_row+first+i)/8]|=1u<<((base_row+first+i)%8);}}
                    else for(size_t i=0;i<count;i++){
                        bool visible=e.visible(first+i);
                        if(!conditions.empty()){
                            uint32_t n;if(view)read(&n,4,row,view);else std::memcpy(&n,scalar_window.get(row,4),4);
                            if(row>e.rows+e.keys||e.rows+e.keys-row<4||n>e.rows+e.keys-row-4)throw std::runtime_error("predicate row outside extent");
                            auto *scalar=view?view+row+4:scalar_window.get(row+4,predicate.read_size(n));row+=4+uint64_t(n);visible=visible&&predicate.matches(scalar,n);
                        }out[i]=visible;
                        if(build&&visible)reinterpret_cast<unsigned char*>(pending.data())[(base_row+first+i)/8]|=1u<<((base_row+first+i)%8);
                    }
                    filter=out;if(!std::memchr(filter,1,count)){first+=count;continue;}
                }
                if(view){spans.push_back({view+e.vectors+8+first*stride,filter,count,quant?quant+(base_row+first)*144:nullptr,indices,index_count,base_row+first});queued+=count;if(queued==block)flush();}
                else{
                    ++buffered_scans;uint64_t offset=e.vectors+8+first*stride;::LogRangeReads reads;
                    auto scan=[&](size_t begin,size_t end){data.resize((end-begin)*stride);read(data.data(),data.size(),offset+begin*stride,nullptr);if(mv_topk_scan(&heap,data.data(),data.size(),filter?filter+begin:nullptr))throw std::runtime_error("buffered log scan failed");};
                    if(reads.plan(filter,count,stride,offset))for(size_t i=0;i<reads.size;i++)scan(reads.runs[i].first,reads.runs[i].last);
                    else scan(0,count);
                }
                first+=count;
            }base_row+=e.count;
        }
        flush();
        if(build)try{bool empty=pending.find_first_not_of(char(0))==pending.npos;if(empty)std::string().swap(pending);std::vector<uint32_t> positions;
            if(postings_enabled&&!empty&&physical<=UINT32_MAX){size_t n=0;for(unsigned char b:pending)n+=__builtin_popcount(unsigned(b));
                size_t overhead=sizeof(Mask)+key.capacity()+pending.capacity()+48;
                if(n&&n<=physical/4&&overhead<mask_limit&&n<=(mask_limit-overhead)/sizeof(uint32_t)){
                    try{positions.reserve(n);for(size_t i=0;i<physical;i++)if((static_cast<unsigned char>(pending[i/8])>>(i%8))&1)positions.push_back(uint32_t(i));}catch(const std::bad_alloc&){std::vector<uint32_t>().swap(positions);}
                }}
            size_t cost=sizeof(Mask)+key.capacity()+pending.capacity()+positions.capacity()*sizeof(uint32_t)+48;if(cost<=mask_limit){while(!masks.empty()&&mask_bytes+cost>mask_limit){mask_bytes-=masks.front().charge;masks.pop_front();}mask_bytes+=cost;masks.push_back({std::move(key),std::move(pending),cost,empty,std::move(positions)});}}catch(const std::bad_alloc&){masks.clear();mask_bytes=0;}
        std::sort(heap.heap.begin(),heap.heap.end());return heap.heap;
    }
    using Key=std::variant<int64_t,std::string,double>;
    struct Ordered {std::vector<Key> keys;int64_t id=0;std::string row;size_t cost=0;};
    static int compare_key(const Key &a,const Key &b){
        if(a.index()!=b.index())throw std::runtime_error("inconsistent sort key type");
        if(auto p=std::get_if<int64_t>(&a)){auto v=std::get<int64_t>(b);return *p<v?-1:*p>v?1:0;}
        if(auto p=std::get_if<double>(&a)){auto v=std::get<double>(b);return *p<v?-1:*p>v?1:0;}
        auto &x=std::get<std::string>(a);auto &y=std::get<std::string>(b);return x<y?-1:x>y?1:0;
    }
    static int compare_ordered(const Ordered &a,const Ordered &b,const std::vector<BoundOrder> &order){
        for(size_t i=0;i<order.size();i++){int c=compare_key(a.keys[i],b.keys[i]);if(c)return order[i].descending?-c:c;}return a.id<b.id?-1:a.id>b.id?1:0;
    }
    Ordered ordering(const Row &row,const Table &table,const std::vector<BoundOrder> &order){
        Ordered out;out.id=std::get<int64_t>(row[table.pk]);out.cost=sizeof(Ordered)+order.size()*sizeof(Key);
        for(auto &item:order){auto &e=item.expr;if(e.distance)out.keys.emplace_back(squared_distance(std::get<std::vector<float>>(row[e.column]),e.query));else if(auto p=std::get_if<int64_t>(&row[e.column]))out.keys.emplace_back(*p);else{auto &v=std::get<std::string>(row[e.column]);out.keys.emplace_back(v);out.cost+=v.size();}}return out;
    }
    std::filesystem::path new_sort_path(){static uint64_t sequence=0;return log.root()/("query-result-sort-"+std::to_string(++sequence));}
    std::unique_ptr<LogResponse> generic_select(const LogTable &source,const Statement &st,std::unique_ptr<LogResponse> response,const std::vector<BoundExpr> &outputs,const std::vector<BoundCondition> &predicate){
        const auto &table=source.schema;std::vector<BoundOrder> order;
        for(auto &item:st.order){std::optional<BoundExpr> expr;
            if(!item.expr.distance)for(size_t i=0;i<response->columns.size();i++)if(fold(response->columns[i])==fold(item.expr.name)){expr=outputs[i];break;}
            if(!expr)expr=bind(table,item.expr);if(!expr->distance&&table.columns[expr->column].type=="VECTOR")fail("cannot order VECTOR");order.push_back({*expr,item.descending});
        }
        std::vector<bool> needed(table.columns.size(),false);needed[table.pk]=true;
        for(auto &c:predicate)needed[c.column]=true;for(auto &e:outputs)needed[e.column]=true;for(auto &o:order)needed[o.expr.column]=true;
        size_t count=0;
        auto visit=[&](auto consume){for(auto &extent:source.extents){
            auto interval=scan_interval(extent,table,predicate);LogBlockScanner scan(log.descriptor(),table,extent,needed,interval.first,interval.second,&predicate);Row row;size_t ordinal;
            while(! (order.empty()&&st.limit&&count>=*st.limit)&&scan.next(row,ordinal))consume(std::move(row));
            if(order.empty()&&st.limit&&count>=*st.limit)return;
        }};
        if(order.empty()){
            visit([&](Row row){if(matches(row,predicate)){response->append(project(row,outputs),log.root());++count;}});response->finish();return response;
        }
        auto better=[&](const Ordered &a,const Ordered &b){return compare_ordered(a,b,order)<0;};
        std::vector<Ordered> selected;size_t memory=0;bool bounded=st.limit&&*st.limit<=1024;
        struct Runs {std::vector<std::filesystem::path> paths;~Runs(){for(auto &p:paths){std::error_code ec;std::filesystem::remove(p,ec);}}} garbage;
        std::vector<std::filesystem::path> runs;
        auto encode=[](const Ordered &item){Json out=Json::object(),keys=Json::array();out["id"]=item.id;out["row"]=item.row;for(auto &k:item.keys)std::visit([&](auto &v){keys.push(v);},k);out["keys"]=keys;return out.dump();};
        auto decode=[](const std::string &line){auto value=Json::parse(line);Ordered out;out.id=value.at("id").integer();out.row=value.at("row").string();for(auto &k:value.at("keys").items()){if(k.is_string())out.keys.emplace_back(k.string());else if(k.is_int())out.keys.emplace_back(k.integer());else out.keys.emplace_back(k.number());}return out;};
        auto new_run=[&](){auto p=new_sort_path();garbage.paths.push_back(p);return p;};
        auto flush=[&](){if(selected.empty())return;std::sort(selected.begin(),selected.end(),better);auto path=new_run();std::ofstream out(path);if(!out)throw std::runtime_error("open sorting run");for(auto &item:selected)out<<encode(item)<<'\n';out.close();if(!out)throw std::runtime_error("write sorting run");runs.push_back(path);selected.clear();memory=0;};
        visit([&](Row row){
            if(!matches(row,predicate))return;Ordered item=ordering(row,table,order);
            if(bounded&&selected.size()==*st.limit&&!better(item,selected.front()))return;
            item.row=project(row,outputs);item.cost+=item.row.size();
            if(bounded&&selected.size()==*st.limit){std::pop_heap(selected.begin(),selected.end(),better);memory-=selected.back().cost;selected.pop_back();}
            memory+=item.cost;selected.push_back(std::move(item));if(bounded)std::push_heap(selected.begin(),selected.end(),better);
            if(memory>(512u<<10)){flush();bounded=false;}
        });
        if(runs.empty()){std::sort(selected.begin(),selected.end(),better);for(auto &item:selected){if(st.limit&&count>=*st.limit)break;response->append(std::move(item.row),log.root());++count;}response->finish();return response;}
        flush();
        auto merge=[&](const std::vector<std::filesystem::path> &inputs,const std::optional<std::filesystem::path> &output){
            std::vector<std::ifstream> streams(inputs.size());struct Head{Ordered item;size_t source;};
            auto later=[&](const Head &a,const Head &b){return better(b.item,a.item);};std::vector<Head> heap;
            auto read=[&](size_t i){std::string line;if(std::getline(streams[i],line)){heap.push_back({decode(line),i});std::push_heap(heap.begin(),heap.end(),later);}else if(!streams[i].eof())throw std::runtime_error("read sorting run");};
            for(size_t i=0;i<inputs.size();i++){streams[i].open(inputs[i]);if(!streams[i])throw std::runtime_error("open sorting input");read(i);}
            std::ofstream sink;if(output){sink.open(*output);if(!sink)throw std::runtime_error("open merged run");}
            while(!heap.empty()&&(output||!st.limit||count<*st.limit)){std::pop_heap(heap.begin(),heap.end(),later);Head head=std::move(heap.back());heap.pop_back();if(output)sink<<encode(head.item)<<'\n';else{response->append(std::move(head.item.row),log.root());++count;}read(head.source);}
            if(output){sink.close();if(!sink)throw std::runtime_error("write merged run");}
        };
        while(runs.size()>8){std::vector<std::filesystem::path> next;for(size_t first=0;first<runs.size();first+=8){std::vector<std::filesystem::path> group(runs.begin()+first,runs.begin()+std::min(runs.size(),first+8));auto output=new_run();merge(group,output);next.push_back(output);for(auto &p:group)std::filesystem::remove(p);}runs=std::move(next);}
        merge(runs,std::nullopt);response->finish();return response;
    }    size_t insert(const std::string &name,Bytes batch,std::vector<int64_t> ids){
        auto &table=tables.at(name);if(ids.empty()||!std::is_sorted(ids.begin(),ids.end())||std::adjacent_find(ids.begin(),ids.end())!=ids.end())fail("duplicate primary key","duplicate_key");
        // Validate a batch against each intersecting extent once. Never decode
        // row values merely to test primary-key existence.
        for(auto &e:table.extents){
            if(ids.back()<e.minimum||ids.front()>e.maximum)continue;
            auto first=std::lower_bound(ids.begin(),ids.end(),e.minimum);
            for(;first!=ids.end()&&*first<=e.maximum;++first){
                size_t lo=0,hi=e.count;
                while(lo<hi){size_t mid=lo+(hi-lo)/2;int64_t key;read_at(log.descriptor(),&key,8,e.rows+e.keys+8+mid*16);if(key<*first)lo=mid+1;else hi=mid;}
                if(lo<e.count&&e.visible(lo)){int64_t key;read_at(log.descriptor(),&key,8,e.rows+e.keys+8+lo*16);if(key==*first)fail("duplicate primary key","duplicate_key");}
            }
        }invalidate();auto extent=log.append(batch,table.schema.dimension(),ids.size());table.extents.push_back(extent);table.schema.count+=ids.size();publish();return ids.size();
    }
public:
    explicit LogDatabase(std::filesystem::path root):log(std::move(root)),tables(std::move(log.recovered)),committed(tables){mv_storage_set_durable(0);
        uint64_t limit=memory_limit();resident_budget=limit-limit/8;
    }
    ~LogDatabase(){
        if(std::getenv("MINIDB_DIAGNOSTICS"))std::fprintf(stderr,"BOUND_PRECISION_PATH {\"fp64\":%llu,\"legacy\":%llu}\n",(unsigned long long)mv_fp64_bound_calls(),(unsigned long long)mv_legacy_bound_calls());
        if(std::getenv("MINIDB_DIAGNOSTICS"))std::fprintf(stderr,"REFINE_FOUR_PATH {\"calls\":%llu,\"points\":%llu}\n",(unsigned long long)mv_refine_four_calls(),(unsigned long long)mv_refine_four_points());
        if(std::getenv("MINIDB_DIAGNOSTICS"))std::fprintf(stderr,"SPARSE_QUERY_PATH {\"scans\":%llu,\"zero_scans\":%llu,\"pairs\":%llu}\n",(unsigned long long)mv_sparse_scan_calls(),(unsigned long long)mv_sparse_zero_calls(),(unsigned long long)mv_sparse_pair_total());
        if(std::getenv("MINIDB_DIAGNOSTICS"))std::fprintf(stderr,"UNIFORM_PATH {\"scans\":%llu}\n",(unsigned long long)uniform_scans);
        if(std::getenv("MINIDB_DIAGNOSTICS"))std::fprintf(stderr,"TILE_BOUND_PATH {\"scans\":%llu}\n",(unsigned long long)mv_tile_bound_calls());
        if(std::getenv("MINIDB_DIAGNOSTICS"))std::fprintf(stderr,"BLOCK_PATH {\"kernel\":\"%s\",\"scans\":%llu,\"builds\":%llu,\"refined\":%llu,\"peak_bytes\":%llu,\"slots\":%llu,\"groups\":%llu,\"certified\":%llu,\"coarse_candidates\":%llu}\n",mv_block_kernel(),(unsigned long long)block_scans,(unsigned long long)block_builds,(unsigned long long)block_refined,(unsigned long long)block_peak,(unsigned long long)block_slots,(unsigned long long)block_groups,(unsigned long long)block_certified,(unsigned long long)block_precise_candidates);
        if(std::getenv("MINIDB_DIAGNOSTICS"))std::fprintf(stderr,"MINIDB_PATH {\"plan_hits\":%llu,\"batch_hits\":%llu,\"plan_builds\":%llu,\"budget_bytes\":%llu,\"mapped_opens\":%llu,\"quant_scan_calls\":%llu,\"quant_builds\":%llu,\"quant_hits\":%llu,\"quant_peak_bytes\":%llu,\"quant_budget_rejects\":%llu,\"quant_numeric_rejects\":%llu,\"posting_scans\":%llu,\"buffered_scans\":%llu,\"integer_hits\":%llu,\"simd\":%d,\"compact_vectors\":%llu}\n",(unsigned long long)plan_hits,(unsigned long long)batch_hits,(unsigned long long)plan_builds,(unsigned long long)resident_budget,(unsigned long long)map_opens,(unsigned long long)mv_quant_scan_calls(),(unsigned long long)quant_builds,(unsigned long long)quant_hits,(unsigned long long)quant_peak,(unsigned long long)quant_budget_rejects,(unsigned long long)quant_numeric_rejects,(unsigned long long)posting_scans,(unsigned long long)buffered_scans,(unsigned long long)integer_hits,mv_screen_simd(),(unsigned long long)compact_vector_accepts);
        invalidate();}
    void close(){invalidate();tables=committed;log.rollback();transaction=false;}
    std::unique_ptr<LogResponse> execute(std::string_view sql,const std::string &mode="exact"){
        if(mode!="exact"&&mode!="ann")fail("invalid mode","invalid_request");auto response=std::make_unique<LogResponse>();
        if(prepared_select(sql,*response))return response;
        {Parser head(sql);if(head.take("INSERT")&&head.take("INTO")){
            auto name=fold(head.ident());auto it=tables.find(name);if(it==tables.end())fail("unknown table","unknown_table");auto &table=it->second.schema;std::vector<mv_column> cols;
            bool eligible=true;
            for(auto &c:table.columns){if(c.n>UINT32_MAX||c.name.find(char(0))!=c.name.npos||fold(c.name)!=c.name||std::any_of(c.name.begin(),c.name.end(),[](unsigned char b){return b>=128;})){eligible=false;break;}cols.push_back({c.name.c_str(),c.type=="BIGINT"?0u:c.type=="VARCHAR"?1u:2u,uint32_t(c.n)});}
            if(eligible){auto tail=sql.substr(head.position());std::unique_ptr<::Batch,void(*)(void*)> batch(static_cast<::Batch*>(mv_insert_prepare(tail.data(),tail.size(),cols.data(),cols.size(),table.pk)),mv_insert_free);
                if(batch){response->status["affected_rows"]=insert(name,std::move(batch->data),std::move(batch->ids));return response;}}

        }}
        Statement st=Parser(sql).parse();
        if(st.op=="begin"){if(transaction)fail("nested transaction","invalid_transaction");transaction=true;return response;}
        if(st.op=="commit"){if(transaction){invalidate();log.commit(tables,committed);committed=tables;transaction=false;collect();}return response;}
        if(st.op=="rollback"){close();return response;}
        if(st.op=="create"){
            std::string name=fold(st.table);if(tables.count(name))fail("table exists","already_exists");LogTable next;next.schema.columns=st.columns;size_t pks=0,vectors=0;std::set<std::string> names;
            for(size_t i=0;i<st.columns.size();i++){auto &c=st.columns[i];if(c.n>SIZE_MAX/4||c.n>size_t(INT64_MAX))fail("column width out of range","invalid_value");if(!names.insert(fold(c.name)).second)fail("duplicate column");if(c.primary){++pks;next.schema.pk=i;}if(c.type=="VECTOR"){++vectors;next.schema.vector_column=i;}}
            if(pks!=1||next.schema.columns[next.schema.pk].type!="BIGINT"||vectors>1)fail("invalid schema");invalidate();tables.emplace(name,std::move(next));publish();return response;
        }
        auto table_it=tables.find(fold(st.table));if(table_it==tables.end())fail("unknown table","unknown_table");auto &table=table_it->second;auto &schema=table.schema;
        if(st.op=="index"){if(schema.columns[schema.column(st.index_column)].type!="VECTOR")fail("index requires vector");for(auto &name:schema.indexes)if(fold(name)==fold(st.index))fail("index exists","already_exists");invalidate();schema.indexes.push_back(st.index);publish();return response;}
        if(st.op=="insert"){
            std::vector<size_t> indices;if(st.insert_columns){for(auto &n:st.insert_names)indices.push_back(schema.column(n));}else for(size_t i=0;i<schema.columns.size();i++)indices.push_back(i);
            std::set<size_t> unique(indices.begin(),indices.end());if(unique.size()!=indices.size()||indices.size()!=schema.columns.size())fail("INSERT requires all columns");
            std::vector<std::pair<int64_t,Row>> rows;for(auto &values:st.rows){if(values.size()!=indices.size())fail("INSERT value count mismatch");Row row(schema.columns.size());for(size_t i=0;i<indices.size();i++)row[indices[i]]=convert(schema.columns[indices[i]],values[i]);rows.emplace_back(std::get<int64_t>(row[schema.pk]),std::move(row));}
            std::sort(rows.begin(),rows.end(),[](auto &a,auto &b){return a.first<b.first;});Bytes payload;std::vector<int64_t> ids;
            for(auto &[id,row]:rows){ids.push_back(id);Bytes scalar=Codec{schema}.scalars(row);if(scalar.size()>UINT32_MAX)fail("row too large","invalid_value");::put(payload,id);uint32_t n=scalar.size();::put(payload,n);payload.insert(payload.end(),scalar.begin(),scalar.end());if(schema.vector_column>=0){auto &v=std::get<std::vector<float>>(row[schema.vector_column]);auto p=reinterpret_cast<const unsigned char*>(v.data());payload.insert(payload.end(),p,p+v.size()*4);}}
            response->status["affected_rows"]=insert(fold(st.table),std::move(payload),std::move(ids));return response;
        }
        if(st.op=="update"||st.op=="delete"){response->status["affected_rows"]=mutate(table,st);return response;}
        if(st.op!="select")fail("queryable-log prototype does not yet implement this statement");
        response->query=true;auto predicate=conditions(schema,st);std::vector<BoundExpr> outputs;
        if(st.star){for(size_t i=0;i<schema.columns.size();i++){outputs.push_back({i,false,{}});response->columns.push_back(schema.columns[i].name);}}
        else for(auto &p:st.projection){auto expr=bind(schema,p.expr);response->columns.push_back(p.alias&&!p.alias->empty()?*p.alias:expr.distance?"L2_DISTANCE":schema.columns[expr.column].name);outputs.push_back(std::move(expr));}
        if(st.limit&&!*st.limit)return response;
        if(st.order.empty())for(auto &c:predicate)if(c.column==schema.pk&&c.op=="="){
            bool vectors=false;for(auto &e:outputs)if(e.distance||int(e.column)==schema.vector_column)vectors=true;std::vector<bool> needed;if(selective_lookup_enabled){needed.assign(schema.columns.size(),false);for(auto &e:outputs)needed[e.column]=true;for(auto &condition:predicate)needed[condition.column]=true;}auto row=lookup(table,std::get<int64_t>(c.value),vectors,mapped(table),true,needed.empty()?nullptr:&needed);
            if(row&&matches(*row,predicate))response->append(project(*row,outputs),log.root());response->finish();return response;
        }
        if(!st.limit||*st.limit>1024||st.order.size()!=1||st.order[0].descending)return generic_select(table,st,std::move(response),outputs,predicate);
        std::optional<BoundExpr> order;if(!st.order[0].expr.distance){auto name=fold(st.order[0].expr.name);for(size_t i=0;i<response->columns.size();i++)if(fold(response->columns[i])==name){order=outputs[i];break;}}
        if(!order)order=bind(schema,st.order[0].expr);if(!order->distance)return generic_select(table,st,std::move(response),outputs,predicate);
        bool require_scores=std::any_of(outputs.begin(),outputs.end(),[](auto &e){return e.distance;});auto ranked=topk(fold(st.table),table,*order,*st.limit,predicate,require_scores);bool covering=true,vectors=false;
        for(auto &e:outputs){bool known=e.distance&&e.column==order->column&&e.query==order->query;if(e.distance?!known:e.column!=schema.pk)covering=false;if(int(e.column)==schema.vector_column&&!known)vectors=true;}
        bool batched=false;
        if(batch_output_enabled&&outputs.size()<=8){try{std::vector<unsigned char> fields;for(auto &output:outputs){int field=projection_field(schema,output,*order,predicate);if(field<0){fields.clear();break;}fields.push_back(field);}if(!fields.empty()){for(size_t i=0;i<std::min(size_t(8),predicate.size());i++)if(std::holds_alternative<int64_t>(predicate[i].value))response->constants[i]=std::get<int64_t>(predicate[i].value);auto projection=compile_projection(response->columns,outputs,&fields);batched=response->adopt_compiled(projection,ranked);}}catch(const std::bad_alloc&){}if(batched)++batch_hits;}
        std::vector<bool> needed;if(!batched&&!covering&&selective_lookup_enabled){needed.assign(schema.columns.size(),false);for(auto &e:outputs)if(!e.distance||e.column!=order->column||e.query!=order->query)needed[e.column]=true;for(auto &condition:predicate)needed[condition.column]=true;}
        if(!batched)for(auto &[score,id]:ranked){
            if(covering){std::string row="[";for(size_t i=0;i<outputs.size();i++){if(i)row+=',';row+=outputs[i].distance?real(std::sqrt(score)):std::to_string(id);}response->append(row+"]",log.root());}
            else{auto row=lookup(table,id,vectors,mapped(table),true,needed.empty()?nullptr:&needed);if(!row||!matches(*row,predicate))throw std::runtime_error("invalid queryable TopK row");response->append(project(*row,outputs,&*order,score),log.root());}
        }response->finish();try{remember_select(sql,st,schema,*order,outputs,predicate,*response);}catch(const std::bad_alloc&){}return response;
    }
};
}
