#pragma once
#include "store.hpp"
#include <list>
namespace cppdb {
struct BoundExpr {size_t column=0;bool distance=false;std::vector<double> query;};
struct BoundCondition {size_t column;std::string op;Cell value;};
struct BoundOrder {BoundExpr expr;bool descending;};
inline int compare_cell(const Cell &a,const Cell &b){
    if(auto p=std::get_if<int64_t>(&a)){auto q=std::get<int64_t>(b);return *p<q?-1:*p>q?1:0;}
    auto &x=std::get<std::string>(a);auto &y=std::get<std::string>(b);return x<y?-1:x>y?1:0;
}
inline bool accepts(int cmp,const std::string &op){return op=="="?cmp==0:op=="<"?cmp<0:op=="<="?cmp<=0:op==">"?cmp>0:cmp>=0;}
inline bool matches(const Row &row,const std::vector<BoundCondition> &conditions){for(auto &c:conditions)if(!accepts(compare_cell(row[c.column],c.value),c.op))return false;return true;}
inline double squared_distance(const std::vector<float> &vector,const std::vector<double> &query){
    if(vector.size()!=query.size())throw std::runtime_error("stored dimension mismatch");::PositiveSum sum;
    for(size_t i=0;i<vector.size();i++){double delta=double(vector[i])-query[i];sum.add(delta*delta);}return sum.result();
}
inline Cell scalar_field(const Table &table,const Bytes &scalar,size_t target){
    size_t pos=0;
    for(size_t i=0;i<table.columns.size();i++){
        auto &c=table.columns[i];if(c.type=="VECTOR")continue;
        if(c.type=="BIGINT"){if(pos>scalar.size()||scalar.size()-pos<8)throw std::runtime_error("incomplete scalar integer");if(i==target){int64_t x;std::memcpy(&x,scalar.data()+pos,8);return x;}pos+=8;}
        else{if(pos>scalar.size()||scalar.size()-pos<4)throw std::runtime_error("incomplete scalar text");uint32_t n;std::memcpy(&n,scalar.data()+pos,4);pos+=4;if(n>scalar.size()-pos)throw std::runtime_error("incomplete scalar text");if(i==target)return std::string(reinterpret_cast<const char*>(scalar.data()+pos),n);pos+=n;}
    }throw std::runtime_error("missing scalar field");
}
struct Response {
    Json status=Json::object();bool query=false;std::vector<std::string> columns,rows;size_t memory=0;
    std::filesystem::path spill;std::unique_ptr<::File> writer;
    ~Response(){writer.reset();if(!spill.empty()){std::error_code ec;std::filesystem::remove(spill,ec);}}
    void append(std::string row,Store &store){
        memory+=row.size()+sizeof(std::string);
        if(!writer&&memory>65536){spill=store.root()/store.temporary("result");writer=std::make_unique<::File>(spill.c_str(),"wbx");for(auto &s:rows){writer->write(s.data(),s.size());writer->write("\n",1);}rows.clear();}
        if(writer){writer->write(row.data(),row.size());writer->write("\n",1);}else rows.push_back(std::move(row));
    }
    void finish(){if(writer){writer->close();writer.reset();}}
};
class Database {
    Store store;
    struct Mapping {void *data=nullptr;size_t bytes=0;bool constant_checked=false;std::vector<float> point;~Mapping(){if(data)::munmap(data,bytes);}};
    std::map<std::string,std::unique_ptr<Mapping>> mappings;size_t mapped_bytes=0,constant_bytes=0;
    struct Mask {std::string key,bytes;bool empty=false,packed=false;size_t charge=0;};std::list<Mask> masks;size_t mask_bytes=0;uint64_t cache_epoch=0;
    size_t block_target=4<<20;uint64_t mapping_resident_budget=0;
    void invalidate(){mappings.clear();mapped_bytes=0;masks.clear();mask_bytes=0;constant_bytes=0;cache_epoch=store.epoch;}
    BoundExpr bind(const Table &table,const Expr &expr){
        BoundExpr out;out.column=table.column(expr.name);out.distance=expr.distance;
        if(expr.distance){if(table.columns[out.column].type!="VECTOR")fail("L2_DISTANCE requires VECTOR","invalid_vector");auto vector=vector_value(expr.query,table.columns[out.column].n);out.query.assign(vector.begin(),vector.end());}return out;
    }
    std::vector<BoundCondition> bind_conditions(const Table &table,const std::vector<Condition> &conditions){
        std::vector<BoundCondition> out;for(auto &c:conditions){size_t index=table.column(c.column);out.push_back({index,c.op,convert(table.columns[index],c.value,true)});}return out;
    }
    bool scalar_matches(const Table &table,const Bytes &scalar,const std::vector<BoundCondition> &conditions){
        for(auto &c:conditions)if(!accepts(compare_cell(scalar_field(table,scalar,c.column),c.value),c.op))return false;return true;
    }
    void evict_mapping(std::map<std::string,std::unique_ptr<Mapping>>::iterator old){
        mapped_bytes-=old->second->bytes;constant_bytes-=old->second->point.capacity()*sizeof(float);mappings.erase(old);
    }
    Mapping *mapping(const Part &part,size_t stride,bool admit_large){
        auto it=mappings.find(part.vectors);if(it!=mappings.end())return it->second.get();
        Descriptor fd(::open((store.root()/part.vectors).c_str(),O_RDONLY));if(fd.fd<0)::error("open vector index");struct stat st;if(::fstat(fd.fd,&st))::error("stat vector index");
        if(part.count>(SIZE_MAX-8)/stride||uint64_t(st.st_size)!=8+part.count*stride)throw std::runtime_error("invalid index record count");size_t bytes=st.st_size;constexpr size_t budget=64u<<20;
        bool enabled=!(std::getenv("MINIDB_INDEX_MMAP")&&std::string(std::getenv("MINIDB_INDEX_MMAP"))=="0");
        size_t wanted=enabled&&bytes<=(admit_large?budget:(8u<<20))?bytes:0;
        while(!mappings.empty()&&(mapped_bytes+wanted>budget||mappings.size()>=16))evict_mapping(mappings.begin());
        auto value=std::make_unique<Mapping>();
        if(wanted){void *ptr=::mmap(nullptr,bytes,PROT_READ,MAP_PRIVATE,fd.fd,0);
            if(ptr!=MAP_FAILED){value->data=ptr;value->bytes=bytes;if(std::memcmp(ptr,"MVFLT001",8))throw std::runtime_error("invalid vector header");}}
        auto *out=value.get();mapped_bytes+=value->bytes;mappings.emplace(part.vectors,std::move(value));return out;
    }
    std::string mask_key(const Part &part,const std::vector<BoundCondition> &conditions){
        std::string key=part.vectors;for(auto &c:conditions){key+='|';key+=std::to_string(c.column);key+=c.op;key+=cell_json(c.value);}return key;
    }
    void remember_constant(Mapping &index,const unsigned char *point,size_t dimension){
        size_t bytes=dimension*sizeof(float);if(bytes>(64u<<10))return;
        while(constant_bytes+bytes>(64u<<10)){
            auto old=mappings.begin();if(old!=mappings.end()&&old->second.get()==&index)++old;if(old==mappings.end())return;evict_mapping(old);
        }
        std::vector<float> value(dimension);std::memcpy(value.data(),point,bytes);constant_bytes+=value.capacity()*sizeof(float);index.point=std::move(value);
    }
    __attribute__((noinline)) void discover_constant(const Part &part,size_t dimension,Mapping &index){
        index.constant_checked=true;if(part.count<32)return;size_t width=dimension*4,stride=width+8;
        if(index.data){
            auto *data=static_cast<const unsigned char*>(index.data)+8;auto *point=data+8;
            for(size_t i=1;i<part.count;i++)if(std::memcmp(point,data+i*stride+8,width))return;
            remember_constant(index,point,dimension);
        }else{
            ::File input((store.root()/part.vectors).c_str(),"rb");char magic[8];input.read(magic,8);if(std::memcmp(magic,"MVFLT001",8))throw std::runtime_error("invalid vector header");
            Bytes first(stride),record(stride);input.read(first.data(),stride);
            for(size_t i=1;i<part.count;i++){input.read(record.data(),stride);if(std::memcmp(first.data()+8,record.data()+8,width))return;}
            remember_constant(index,first.data()+8,dimension);
        }
    }
    __attribute__((noinline)) void constant_topk(const Table &table,const Part &part,const std::vector<float> &point,const BoundExpr &expr,::TopK &heap,const std::vector<BoundCondition> &conditions,const std::optional<std::pair<int64_t,int64_t>> &bounds,const std::string &mask_name,const Mask *cached,bool build,bool packed){
        double score=squared_distance(point,expr.query);
        // A whole segment can lose on distance/PK without implying its filter
        // is empty. Never cache that query-dependent decision as an empty mask.
        if(heap.heap.size()==heap.k&&std::make_pair(score,part.minimum)>=heap.heap.front())return;
        size_t found=0;
        if(conditions.empty()||cached){
            if(cached){size_t expected=cached->packed?part.count/8+(part.count%8!=0):part.count;if(cached->bytes.size()!=expected)throw std::runtime_error("invalid constant mask");}
            Descriptor keys(::open((store.root()/part.keys).c_str(),O_RDONLY));if(keys.fd<0)::error("open constant primary keys");char magic[8];read_at(keys.fd,magic,8,part.key_offset);
            size_t stride;if(!std::memcmp(magic,"MVKEY002",8))stride=16;else if(!std::memcmp(magic,"MVKEY001",8))stride=8;else throw std::runtime_error("invalid primary key header");
            Bytes block;for(size_t first=0;first<part.count&&found<heap.k;){size_t count=std::min(size_t(256),part.count-first);block.resize(count*stride);read_at(keys.fd,block.data(),block.size(),part.key_offset+8+first*stride);
                for(size_t i=0;i<count&&found<heap.k;i++){
                    if(cached){size_t row=first+i;unsigned char byte=cached->bytes[cached->packed?row/8:row];if(!(cached->packed?(byte>>(row%8))&1:byte))continue;}
                    int64_t id;std::memcpy(&id,block.data()+i*stride,8);if(std::binary_search(part.excluded.begin(),part.excluded.end(),id))continue;heap.offer_score(score,id);++found;}first+=count;}
        }else if(build){
            std::string pending(packed?part.count/8+(part.count%8!=0):part.count,char(0));
            ::File rows((store.root()/part.file).c_str(),"rb");char magic[8];rows.read(magic,8);if(std::memcmp(magic,"MVROW001",8))throw std::runtime_error("invalid scalar header");Bytes scalar;
            for(size_t row=0;row<part.count;row++){
                uint32_t n;rows.read(&n,4);scalar.resize(n);rows.read(scalar.data(),n);bool visible=true;int64_t id=0;
                if(!part.excluded.empty()){id=std::get<int64_t>(scalar_field(table,scalar,table.pk));visible=!std::binary_search(part.excluded.begin(),part.excluded.end(),id);}
                if(visible)visible=scalar_matches(table,scalar,conditions);if(!visible)continue;
                if(packed)reinterpret_cast<unsigned char*>(pending.data())[row/8]|=static_cast<unsigned char>(1u<<(row%8));else pending[row]=1;
                if(found<heap.k){if(part.excluded.empty())id=std::get<int64_t>(scalar_field(table,scalar,table.pk));heap.offer_score(score,id);++found;}
            }
            remember_mask(mask_name,std::move(pending),packed);return;
        }else{
            Table one=table;one.parts={part};Scanner scan(store.root(),one,false,bounds);int64_t id;Bytes scalar,vector;
            while(found<heap.k&&scan.next(id,scalar,vector))if(scalar_matches(table,scalar,conditions)){heap.offer_score(score,id);++found;}
        }
        if(!found&&!mask_name.empty())remember_mask(mask_name,std::string(),false);
    }
    Mask *find_mask(const std::string &key){for(auto it=masks.begin();it!=masks.end();++it)if(it->key==key){masks.splice(masks.end(),masks,it);return &masks.back();}return nullptr;}
    void remember_mask(std::string key,std::string bytes,bool packed){
        bool empty=bytes.find_first_not_of(char(0))==bytes.npos;if(empty)std::string().swap(bytes);
        size_t cost=key.capacity()+bytes.capacity()+sizeof(Mask)+2*sizeof(void*)+32;if(cost>(128u<<10))return;
        while(!masks.empty()&&mask_bytes+cost>(128u<<10)){mask_bytes-=masks.front().charge;masks.pop_front();}
        mask_bytes+=cost;masks.push_back({std::move(key),std::move(bytes),empty,packed,cost});
    }
    static void unpack_mask(const std::string &encoded,size_t first,size_t count,Bytes &out){
        static constexpr auto expand=[]() constexpr{std::array<std::array<unsigned char,8>,256> table{};
            for(size_t byte=0;byte<256;byte++)for(size_t bit=0;bit<8;bit++)table[byte][bit]=(byte>>bit)&1;return table;}();
        out.resize(count);auto *source=reinterpret_cast<const unsigned char*>(encoded.data());size_t done=0;
        for(;done+8<=count;done+=8){size_t pos=first+done,index=pos/8,shift=pos%8;unsigned byte=source[index]>>shift;
            if(shift)byte|=unsigned(source[index+1])<<(8-shift);std::memcpy(out.data()+done,expand[byte&255].data(),8);}
        for(;done<count;done++){size_t pos=first+done;out[done]=(source[pos/8]>>(pos%8))&1;}
    }
    std::vector<std::pair<double,int64_t>> vector_topk(const Table &table,const BoundExpr &expr,size_t k,const std::vector<BoundCondition> &conditions,const std::optional<std::pair<int64_t,int64_t>> &bounds={}){
        if(cache_epoch!=store.epoch)invalidate();::TopK heap(expr.query.data(),expr.query.size(),k);size_t stride=8+expr.query.size()*4;
        if(bounds){
            uint64_t span=uint64_t(bounds->second)-uint64_t(bounds->first),limit=std::min(uint64_t(4096),uint64_t(table.count/64));
            // Unique integer primary keys bound the number of visible rows
            // by interval width, without a sampled selectivity estimate.
            if(span<limit){
                Scanner scan(store.root(),table,true,bounds);int64_t id;Bytes scalar,vector,record;
                while(scan.next(id,scalar,vector)){
                    if(!scalar_matches(table,scalar,conditions))continue;record.resize(8+vector.size());std::memcpy(record.data(),&id,8);std::memcpy(record.data()+8,vector.data(),vector.size());heap.offer(record.data());
                }
                std::sort(heap.heap.begin(),heap.heap.end());return heap.heap;
            }
        }
        if(expr.query.size()>4096){Scanner scan(store.root(),table);int64_t id;Bytes scalar,vector;while(scan.next(id,scalar,vector)){
            if(!scalar_matches(table,scalar,conditions))continue;Bytes record(8+vector.size());std::memcpy(record.data(),&id,8);std::memcpy(record.data()+8,vector.data(),vector.size());heap.offer(record.data());
        }}else {
            // Admit larger mappings only if all physical vector records fit
            // within7/8 of the cgroup budget. Count excluded rows too: their
            // immutable files still occupy pages while scanning.
            uint64_t remaining=mapping_resident_budget;bool admit_large=remaining!=0;
            for(auto &part:table.parts){if(remaining<8||part.count>(remaining-8)/stride){admit_large=false;break;}remaining-=8+part.count*stride;}
            for(auto &part:table.parts){
            bool filtering=!conditions.empty()||!part.excluded.empty();std::string key=filtering?mask_key(part,conditions):"";Mask *cached=filtering?find_mask(key):nullptr;if(cached&&cached->empty)continue;
            bool packed=part.count>=256;size_t cache_bytes=packed?part.count/8+(part.count%8!=0):part.count;
            bool build=filtering&&!cached&&cache_bytes+key.size()+sizeof(Mask)<=(128u<<10);std::string pending;
            auto *index=mapping(part,stride,admit_large);if(!index->constant_checked&&conditions.empty())discover_constant(part,expr.query.size(),*index);
            if(!index->point.empty()){constant_topk(table,part,index->point,expr,heap,conditions,bounds,key,cached,build,packed);continue;}
            if(build){if(packed)pending.assign(cache_bytes,char(0));else pending.reserve(part.count);}
            std::unique_ptr<::File> rows;if(filtering&&!cached&&!conditions.empty()){rows=std::make_unique<::File>((store.root()/part.file).c_str(),"rb");char magic[8];rows->read(magic,8);if(std::memcmp(magic,"MVROW001",8))throw std::runtime_error("invalid scalar header");}
            auto *mapped=index->data?index:nullptr;std::unique_ptr<::File> input;if(!mapped){input=std::make_unique<::File>((store.root()/part.vectors).c_str(),"rb");char magic[8];input->read(magic,8);if(std::memcmp(magic,"MVFLT001",8))throw std::runtime_error("invalid index header");}
            size_t records=std::max(size_t(1),block_target/stride);Bytes data,mask,scalar;
            for(size_t first=0;first<part.count;){
                size_t count=std::min(records,part.count-first),bytes=count*stride;
                const unsigned char *block=mapped?static_cast<const unsigned char*>(mapped->data)+8+first*stride:nullptr;
                auto load=[&](){if(!block){data.resize(bytes);input->read(data.data(),bytes);block=data.data();}};
                const unsigned char *filter=nullptr;
                if(cached){
                    size_t expected=cached->packed?part.count/8+(part.count%8!=0):part.count;if(cached->bytes.size()!=expected)throw std::runtime_error("invalid cached mask");
                    if(cached->packed){unpack_mask(cached->bytes,first,count,mask);filter=mask.data();}
                    else filter=reinterpret_cast<const unsigned char*>(cached->bytes.data()+first);
                }
                else if(filtering){
                    if(!rows)load();mask.resize(count);
                    for(size_t i=0;i<count;i++){
                        bool visible=true;
                        if(rows){
                            uint32_t n;rows->read(&n,4);scalar.resize(n);rows->read(scalar.data(),n);
                            if(!part.excluded.empty()){auto id=std::get<int64_t>(scalar_field(table,scalar,table.pk));visible=!std::binary_search(part.excluded.begin(),part.excluded.end(),id);}
                            if(visible)visible=scalar_matches(table,scalar,conditions);
                        }else{int64_t id;std::memcpy(&id,block+i*stride,8);visible=!std::binary_search(part.excluded.begin(),part.excluded.end(),id);}
                        mask[i]=visible;
                        if(build&&packed&&visible)reinterpret_cast<unsigned char*>(pending.data())[(first+i)/8]|=static_cast<unsigned char>(1u<<((first+i)%8));
                    }
                    filter=mask.data();if(build&&!packed)pending.append(reinterpret_cast<const char*>(mask.data()),mask.size());
                }
                if(filter&&!std::memchr(filter,1,count)){
                    if(!block&&::fseeko(input->f,bytes,SEEK_CUR))throw std::runtime_error("filtered block seek failed");first+=count;continue;
                }
                load();if(mv_topk_scan(&heap,block,bytes,filter))throw std::runtime_error("native TopK scan failed");first+=count;
            }
            if(build)remember_mask(std::move(key),std::move(pending),packed);
        }}
        std::sort(heap.heap.begin(),heap.heap.end());return heap.heap;
    }
    static bool same(const BoundExpr &a,const BoundExpr &b){return a.column==b.column&&a.distance==b.distance&&a.query==b.query;}
    std::string project(const Row &row,const std::vector<BoundExpr> &outputs,std::optional<std::pair<const BoundExpr*,double>> known={}){
        std::string out="[";bool first=true;for(auto &expr:outputs){if(!first)out+=',';first=false;if(expr.distance){double value=known&&same(expr,*known->first)?known->second:squared_distance(std::get<std::vector<float>>(row[expr.column]),expr.query);out+=real(std::sqrt(value));}else out+=cell_json(row[expr.column]);}out+=']';return out;
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
    std::unique_ptr<Response> select(const Table &table,const Statement &st){
        auto conditions=bind_conditions(table,st.conditions);std::vector<BoundExpr> outputs;auto response=std::make_unique<Response>();response->query=true;
        if(st.star){for(size_t i=0;i<table.columns.size();i++){outputs.push_back({i,false,{}});response->columns.push_back(table.columns[i].name);}}
        else for(auto &p:st.projection){auto expr=bind(table,p.expr);response->columns.push_back(p.alias&&!p.alias->empty()?*p.alias:expr.distance?"L2_DISTANCE":table.columns[expr.column].name);outputs.push_back(std::move(expr));}
        std::vector<BoundOrder> order;for(auto &o:st.order){std::optional<BoundExpr> bound;if(!o.expr.distance){auto name=fold(o.expr.name);for(size_t i=0;i<response->columns.size();i++)if(fold(response->columns[i])==name){bound=outputs[i];break;}}if(!bound)bound=bind(table,o.expr);if(!bound->distance&&table.columns[bound->column].type=="VECTOR")fail("cannot ORDER BY VECTOR");order.push_back({*bound,o.descending});}
        if(st.limit&&!*st.limit)return response;
        for(auto &c:conditions)if(c.column==table.pk&&c.op=="="){
            auto stored=store.lookup(table,std::get<int64_t>(c.value));if(stored){Row row=Codec{table}.decode(stored->first,stored->second);if(matches(row,conditions))response->append(project(row,outputs),store);}response->finish();return response;
        }
        std::optional<std::pair<int64_t,int64_t>> bounds;int64_t lower=INT64_MIN,upper=INT64_MAX;bool primary_filter=false,empty=false;
        for(auto &c:conditions)if(c.column==table.pk){primary_filter=true;auto value=std::get<int64_t>(c.value);
            if(c.op==">"){if(value==INT64_MAX)empty=true;else lower=std::max(lower,int64_t(value+1));}
            else if(c.op==">=")lower=std::max(lower,value);
            else if(c.op=="<"){if(value==INT64_MIN)empty=true;else upper=std::min(upper,int64_t(value-1));}
            else if(c.op=="<=")upper=std::min(upper,value);
        }
        if(empty||lower>upper)return response;if(primary_filter)bounds=std::make_pair(lower,upper);
        if(st.limit&&*st.limit<=1024&&order.size()==1&&order[0].expr.distance&&!order[0].descending){
            auto &expr=order[0].expr;auto scored=vector_topk(table,expr,*st.limit,conditions,bounds);bool covering=true;for(auto &e:outputs)if(e.distance?!same(e,expr):e.column!=table.pk)covering=false;
            std::vector<bool> wanted;
            if(!covering){wanted.assign(table.columns.size(),false);wanted[table.pk]=true;for(auto &c:conditions)wanted[c.column]=true;for(auto &e:outputs)if(!e.distance||!same(e,expr))wanted[e.column]=true;}
            bool read_vectors=!covering&&table.vector_column>=0&&wanted[table.vector_column];
            for(auto &[score,id]:scored){
                if(covering){std::string row="[";bool first=true;for(auto &e:outputs){if(!first)row+=',';first=false;row+=e.distance?real(std::sqrt(score)):std::to_string(id);}row+=']';response->append(std::move(row),store);}
                else{auto stored=store.lookup(table,id,nullptr,read_vectors);if(!stored)throw std::runtime_error("missing TopK record");Row row=Codec{table}.decode(stored->first,stored->second,&wanted);if(!matches(row,conditions))throw std::runtime_error("invalid TopK predicate result");response->append(project(row,outputs,std::pair<const BoundExpr*,double>{&expr,score}),store);}
            }response->finish();return response;
        }
        std::vector<bool> needed(table.columns.size(),false);needed[table.pk]=true;
        for(auto &c:conditions)needed[c.column]=true;for(auto &e:outputs)needed[e.column]=true;for(auto &o:order)needed[o.expr.column]=true;
        bool vectors=table.vector_column>=0&&needed[table.vector_column];
        bool reverse_pk=st.limit&&*st.limit<=1024&&order.size()==1&&!order[0].expr.distance&&order[0].expr.column==table.pk&&order[0].descending
            &&std::all_of(conditions.begin(),conditions.end(),[&](auto &c){return c.column==table.pk;})
            &&std::all_of(table.parts.begin(),table.parts.end(),[](auto &p){return p.key_offset!=0;});
        Scanner scan(store.root(),table,vectors,bounds,reverse_pk);int64_t id;Bytes scalar,vector;size_t count=0;
        bool ascending_pk=order.size()==1&&!order[0].expr.distance&&order[0].expr.column==table.pk&&!order[0].descending;
        if(order.empty()||ascending_pk||reverse_pk){
            while((!st.limit||count<*st.limit)&&scan.next(id,scalar,vector)){Row row=Codec{table}.decode(scalar,vector,&needed);if(!matches(row,conditions))continue;response->append(project(row,outputs),store);++count;}response->finish();return response;
        }
        auto better=[&](const Ordered &a,const Ordered &b){return compare_ordered(a,b,order)<0;};
        std::vector<Ordered> selected;size_t memory=0;bool bounded=st.limit&&*st.limit<=1024;
        struct Runs {std::vector<std::filesystem::path> paths;~Runs(){for(auto &p:paths){std::error_code ec;std::filesystem::remove(p,ec);}}} garbage;
        std::vector<std::filesystem::path> runs;
        auto encode=[](const Ordered &item){Json out=Json::object(),keys=Json::array();out["id"]=item.id;out["row"]=item.row;for(auto &k:item.keys)std::visit([&](auto &v){keys.push(v);},k);out["keys"]=keys;return out.dump();};
        auto decode=[](const std::string &line){auto value=Json::parse(line);Ordered out;out.id=value.at("id").integer();out.row=value.at("row").string();for(auto &k:value.at("keys").items()){if(k.is_string())out.keys.emplace_back(k.string());else if(k.is_int())out.keys.emplace_back(k.integer());else out.keys.emplace_back(k.number());}return out;};
        auto new_run=[&](){auto p=store.root()/store.temporary("sort");garbage.paths.push_back(p);return p;};
        auto flush=[&](){if(selected.empty())return;std::sort(selected.begin(),selected.end(),better);auto path=new_run();std::ofstream out(path);if(!out)throw std::runtime_error("open sorting run");for(auto &item:selected)out<<encode(item)<<'\n';out.close();if(!out)throw std::runtime_error("write sorting run");runs.push_back(path);selected.clear();memory=0;};
        while(scan.next(id,scalar,vector)){
            Row row=Codec{table}.decode(scalar,vector,&needed);if(!matches(row,conditions))continue;Ordered item=ordering(row,table,order);
            if(bounded&&selected.size()==*st.limit&&!better(item,selected.front()))continue;
            item.row=project(row,outputs);item.cost+=item.row.size();
            if(bounded&&selected.size()==*st.limit){std::pop_heap(selected.begin(),selected.end(),better);memory-=selected.back().cost;selected.pop_back();}
            memory+=item.cost;selected.push_back(std::move(item));if(bounded)std::push_heap(selected.begin(),selected.end(),better);
            if(memory>(512u<<10)){flush();bounded=false;}
        }
        if(runs.empty()){std::sort(selected.begin(),selected.end(),better);for(auto &item:selected){if(st.limit&&count>=*st.limit)break;response->append(std::move(item.row),store);++count;}response->finish();return response;}
        flush();
        auto merge=[&](const std::vector<std::filesystem::path> &inputs,const std::optional<std::filesystem::path> &output){
            std::vector<std::ifstream> streams(inputs.size());struct Head{Ordered item;size_t source;};
            auto later=[&](const Head &a,const Head &b){return better(b.item,a.item);};std::vector<Head> heap;
            auto read=[&](size_t i){std::string line;if(std::getline(streams[i],line)){heap.push_back({decode(line),i});std::push_heap(heap.begin(),heap.end(),later);}else if(!streams[i].eof())throw std::runtime_error("read sorting run");};
            for(size_t i=0;i<inputs.size();i++){streams[i].open(inputs[i]);if(!streams[i])throw std::runtime_error("open sorting input");read(i);}
            std::ofstream sink;if(output){sink.open(*output);if(!sink)throw std::runtime_error("open merged run");}
            while(!heap.empty()&&(output||!st.limit||count<*st.limit)){std::pop_heap(heap.begin(),heap.end(),later);Head head=std::move(heap.back());heap.pop_back();if(output)sink<<encode(head.item)<<'\n';else{response->append(std::move(head.item.row),store);++count;}read(head.source);}
            if(output){sink.close();if(!sink)throw std::runtime_error("write merged run");}
        };
        while(runs.size()>8){std::vector<std::filesystem::path> next;for(size_t first=0;first<runs.size();first+=8){std::vector<std::filesystem::path> group(runs.begin()+first,runs.begin()+std::min(runs.size(),first+8));auto output=new_run();merge(group,output);next.push_back(output);for(auto &p:group)std::filesystem::remove(p);}runs=std::move(next);}
        merge(runs,std::nullopt);response->finish();return response;
    }
public:
    std::unique_ptr<Response> execute(std::string_view sql,const std::string &mode="exact"){
        if(mode!="exact"&&mode!="ann")fail("invalid search mode","invalid_request");
        // Reuse the e5 validated native INSERT parser before constructing an AST.
        {
            Parser head(sql);if(head.take("INSERT")&&head.take("INTO")){
                std::string name=head.ident();auto it=store.tables.find(fold(name));
                if(it!=store.tables.end()){
                    auto &t=it->second;std::vector<mv_column> cols;bool eligible=true;for(auto &c:t.columns){if(c.n>UINT32_MAX||c.name.find(char(0))!=c.name.npos||fold(c.name)!=c.name||std::any_of(c.name.begin(),c.name.end(),[](unsigned char b){return b>=128;})){eligible=false;break;}cols.push_back({c.name.c_str(),c.type=="BIGINT"?0u:c.type=="VARCHAR"?1u:2u,uint32_t(c.n)});}
                    if(eligible){auto tail=sql.substr(head.position());std::unique_ptr<::Batch,void(*)(void*)> batch(static_cast<::Batch*>(mv_insert_prepare(tail.data(),tail.size(),cols.data(),cols.size(),t.pk)),mv_insert_free);
                        if(batch){invalidate();size_t n=store.insert(name,std::move(batch->data),std::move(batch->ids));auto out=std::make_unique<Response>();out->status["affected_rows"]=n;return out;}}
                }
            }
        }
        Statement st=Parser(sql).parse();auto out=std::make_unique<Response>();
        if(st.op=="begin"){store.begin();return out;}if(st.op=="commit"){store.commit();invalidate();return out;}if(st.op=="rollback"){store.rollback();invalidate();return out;}
        if(st.op=="create"){
            auto key=fold(st.table);if(store.tables.count(key))fail("table already exists","already_exists");Table table;table.columns=st.columns;size_t pks=0,vectors=0;std::set<std::string> names;
            for(size_t i=0;i<table.columns.size();i++){auto &c=table.columns[i];if(!names.insert(fold(c.name)).second)fail("duplicate column");if(c.primary){table.pk=i;++pks;}if(c.type=="VECTOR"){table.vector_column=i;++vectors;if(c.n>SIZE_MAX/4)fail("column size out of range","invalid_value");}if(c.n>size_t(INT64_MAX))fail("column size out of range","invalid_value");}
            if(pks!=1||table.columns[table.pk].type!="BIGINT")fail("exactly one BIGINT primary key required");if(vectors>1)fail("at most one VECTOR column allowed");invalidate();store.install(st.table,std::move(table));return out;
        }
        Table &table=store.table(st.table);
        if(st.op=="select")return select(table,st);
        if(st.op=="index"){
            size_t col=table.column(st.index_column);if(table.columns[col].type!="VECTOR")fail("index requires VECTOR");for(auto &name:table.indexes)if(fold(name)==fold(st.index))fail("index already exists","already_exists");Table updated=table;updated.indexes.push_back(st.index);invalidate();store.install(st.table,std::move(updated));return out;
        }
        if(st.op=="insert"){
            std::vector<size_t> indices;if(st.insert_columns){for(auto &n:st.insert_names)indices.push_back(table.column(n));}else for(size_t i=0;i<table.columns.size();i++)indices.push_back(i);
            std::set<size_t> unique(indices.begin(),indices.end());if(unique.size()!=indices.size()||indices.size()!=table.columns.size())fail("INSERT requires all columns");
            std::vector<std::pair<int64_t,Row>> rows;for(auto &values:st.rows){if(values.size()!=indices.size())fail("INSERT value count mismatch");Row row(table.columns.size());for(size_t i=0;i<indices.size();i++)row[indices[i]]=convert(table.columns[indices[i]],values[i]);rows.emplace_back(std::get<int64_t>(row[table.pk]),std::move(row));}
            std::sort(rows.begin(),rows.end(),[](auto &a,auto &b){return a.first<b.first;});Bytes payload;std::vector<int64_t> ids;
            for(auto &[id,row]:rows){ids.push_back(id);Bytes scalar=Codec{table}.scalars(row);if(scalar.size()>UINT32_MAX)fail("row too large","invalid_value");::put(payload,id);uint32_t n=scalar.size();::put(payload,n);payload.insert(payload.end(),scalar.begin(),scalar.end());if(table.vector_column>=0){auto &v=std::get<std::vector<float>>(row[table.vector_column]);auto p=reinterpret_cast<const unsigned char*>(v.data());payload.insert(payload.end(),p,p+v.size()*4);}}
            invalidate();out->status["affected_rows"]=store.insert(st.table,std::move(payload),std::move(ids));return out;
        }
        auto conditions=bind_conditions(table,st.conditions);std::vector<std::pair<size_t,Cell>> updates;
        if(st.op=="update")for(auto &[name,value]:st.sets){size_t i=table.column(name);if(i==table.pk)fail("primary key update unsupported");if(std::any_of(updates.begin(),updates.end(),[&](auto &u){return u.first==i;}))fail("duplicate assignment");updates.emplace_back(i,convert(table.columns[i],value));}
        for(auto &condition:conditions)if(condition.column==table.pk&&condition.op=="="){
            int64_t id=std::get<int64_t>(condition.value);size_t position=0;auto stored=store.lookup(table,id,&position);
            if(!stored){out->status["affected_rows"]=0;return out;}Row row=Codec{table}.decode(stored->first,stored->second);
            if(!matches(row,conditions)){out->status["affected_rows"]=0;return out;}
            if(st.op=="update"){
                bool changed=false;for(auto &[i,value]:updates)if(row[i]!=value){row[i]=value;changed=true;}
                if(changed){invalidate();store.replace_point(st.table,id,position,row);}
            }else{invalidate();store.replace_point(st.table,id,position,std::nullopt);}
            out->status["affected_rows"]=1;return out;
        }
        if(st.op=="delete"&&conditions.empty()){
            auto count=table.count;if(count){Table result=table;result.parts.clear();result.count=0;invalidate();store.install(st.table,std::move(result));}out->status["affected_rows"]=count;return out;
        }
        invalidate();size_t affected=store.rewrite(st.table,[&](Row &row){if(!matches(row,conditions))return 0;if(st.op=="delete")return -1;for(auto &[i,value]:updates)row[i]=value;return 1;});out->status["affected_rows"]=affected;return out;
    }
    explicit Database(std::filesystem::path root):store(std::move(root)){
        std::ifstream limit("/sys/fs/cgroup/memory.max");std::string value;if(!(limit>>value)||value=="max")block_target=256<<10;else{try{auto bytes=std::stoull(value);mapping_resident_budget=bytes-bytes/8;if(bytes<(32u<<20))block_target=256<<10;}catch(...){block_target=256<<10;}}
    }
    void close(){store.rollback();invalidate();}
    // Statement execution and bounded relational ordering follow below.
};
}
