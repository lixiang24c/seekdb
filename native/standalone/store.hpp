#pragma once
#include "journal.hpp"
namespace cppdb {
class Store {
    static std::filesystem::path prepare(std::filesystem::path root){mv_storage_set_durable(0);std::filesystem::create_directories(root);return root;}
    std::filesystem::path directory;Journal log;uint64_t seed[2]{},counter=0;size_t transaction_statements=0;std::vector<Recipe> transaction_recipes;
    std::string fresh(const char *suffix){char name[96];std::snprintf(name,sizeof(name),"mv-%016llx%016llx-%016llx.%s",(unsigned long long)seed[0],(unsigned long long)seed[1],(unsigned long long)++counter,suffix);return name;}
    bool contains(const Table &table,const std::vector<int64_t> &ids){
        for(auto &part:table.parts){
            auto begin=std::lower_bound(ids.begin(),ids.end(),part.minimum),end=std::upper_bound(ids.begin(),ids.end(),part.maximum);if(begin==end)continue;
            Descriptor file(::open((directory/part.keys).c_str(),O_RDONLY));if(file.fd<0)::error("open primary keys");char magic[8];read_at(file.fd,magic,8,part.key_offset);size_t stride;
            if(!std::memcmp(magic,"MVKEY002",8))stride=16;else if(!std::memcmp(magic,"MVKEY001",8))stride=8;else throw std::runtime_error("invalid key directory");
            for(auto it=begin;it!=end;++it){if(std::binary_search(part.excluded.begin(),part.excluded.end(),*it))continue;size_t lo=0,hi=part.count;while(lo<hi){size_t mid=lo+(hi-lo)/2;int64_t value;read_at(file.fd,&value,8,part.key_offset+8+mid*stride);if(value<*it)lo=mid+1;else if(value>*it)hi=mid;else return true;}}
        }return false;
    }
    Part merged(const Table &table,const std::vector<Part> &parts,const Bytes &payload,bool &native){
        auto row=fresh("rows"),vector=fresh("index"),keys=fresh("keys");native=table.vector_column>=0&&table.dimension()<=4096&&parts.size()<=16;
        if(native){
            std::vector<std::string> row_paths,vector_paths;row_paths.reserve(parts.size());vector_paths.reserve(parts.size());std::vector<mv_source> sources;sources.reserve(parts.size());
            for(auto &p:parts){row_paths.push_back((directory/p.file).string());vector_paths.push_back((directory/p.vectors).string());sources.push_back({row_paths.back().c_str(),vector_paths.back().c_str(),p.count,p.excluded.data(),p.excluded.size()});}
            mv_segment result;char message[512];if(mv_storage_merge((directory/row).c_str(),(directory/vector).c_str(),(directory/keys).c_str(),sources.data(),sources.size(),payload.data(),payload.size(),table.dimension(),0,&result,message,sizeof(message)))throw std::runtime_error(message);
            Part p;p.file=row;p.vectors=vector;p.keys=row;p.count=result.count;p.minimum=result.minimum;p.maximum=result.maximum;p.key_offset=result.key_offset;p.columnar=true;return p;
        }
        Table source=table;source.parts=parts;Scanner scan(directory,source);SegmentWriter output(directory,table,row,vector,keys);
        size_t pos=0,width=table.dimension()*4;Bytes scalar,vec,other_scalar,other_vec;int64_t id=0,other_id=0;
        auto next_input=[&](){if(pos==payload.size())return false;if(pos>payload.size()||payload.size()-pos<12)throw std::runtime_error("incomplete input batch");uint32_t n;std::memcpy(&id,payload.data()+pos,8);std::memcpy(&n,payload.data()+pos+8,4);pos+=12;if(n>payload.size()-pos||width>payload.size()-pos-n)throw std::runtime_error("incomplete input row");scalar.assign(payload.begin()+pos,payload.begin()+pos+n);pos+=n;vec.assign(payload.begin()+pos,payload.begin()+pos+width);pos+=width;return true;};
        bool input=next_input(),existing=scan.next(other_id,other_scalar,other_vec);
        while(input||existing){
            if(input&&(!existing||id<other_id)){output.append(id,scalar,vec);input=next_input();}
            else{if(input&&id==other_id)throw std::runtime_error("duplicate merged key");output.append(other_id,other_scalar,other_vec);existing=scan.next(other_id,other_scalar,other_vec);}
        }
        return output.finish();
    }
public:
    Catalog tables,committed;bool in_transaction=false;uint64_t epoch=0;
    explicit Store(std::filesystem::path root):directory(prepare(std::move(root))),log(directory),tables(log.tables),committed(tables){
        size_t received=0;while(received<sizeof(seed)){auto n=::getrandom(reinterpret_cast<unsigned char*>(seed)+received,sizeof(seed)-received,0);if(n<0&&errno==EINTR)continue;if(n<=0)::error("random segment prefix");received+=n;}
        collect();
    }
    const std::filesystem::path &root()const{return directory;}
    std::string temporary(const char *suffix){return fresh(suffix);}
    Table &table(std::string_view name){auto it=tables.find(fold(name));if(it==tables.end())fail("unknown table "+std::string(name),"unknown_table");return it->second;}
    void collect(){
        auto keep=catalog_files(tables),old=catalog_files(committed);keep.insert(old.begin(),old.end());
        for(auto &p:std::filesystem::directory_iterator(directory))if(p.is_regular_file()){
            auto name=p.path().filename().string();if(name.rfind("mv-",0)==0&&!keep.count(name))std::filesystem::remove(p.path());
        }
    }
    void install(const std::string &name,Table table,std::vector<Recipe> recipes={}){
        tables[fold(name)]=std::move(table);
        if(!in_transaction){log.commit(tables,committed,recipes);committed=tables;}else{if(transaction_statements++)transaction_recipes.clear();else transaction_recipes=std::move(recipes);}++epoch;collect();
    }
    void begin(){if(in_transaction)fail("nested transaction","invalid_transaction");in_transaction=true;transaction_statements=0;transaction_recipes.clear();}
    void commit(){if(in_transaction){log.commit(tables,committed,transaction_recipes);committed=tables;in_transaction=false;transaction_statements=0;transaction_recipes.clear();++epoch;collect();}}
    void rollback(){tables=committed;in_transaction=false;transaction_statements=0;transaction_recipes.clear();++epoch;collect();}
    size_t insert(const std::string &name,Bytes payload,std::vector<int64_t> ids){return insert_into(name,table(name),std::move(payload),std::move(ids));}
    size_t insert_into(const std::string &name,Table result,Bytes payload,std::vector<int64_t> ids){
        if(ids.empty())fail("empty INSERT");if(!std::is_sorted(ids.begin(),ids.end())||std::adjacent_find(ids.begin(),ids.end())!=ids.end())fail("duplicate primary key","duplicate_key");
        if(contains(result,ids))fail("duplicate primary key","duplicate_key");size_t added=ids.size(),ratio=added/100,level=0;while(ratio>1){ratio>>=1;++level;}
        std::vector<Part> merging;size_t merge_weight=added;size_t fanout=result.indexes.empty()?4:2;
        while(!result.parts.empty()){
            std::vector<Part> group;while(!result.parts.empty()&&result.parts.back().level<=level&&result.parts.back().count<=merge_weight&&group.size()<fanout-1){group.push_back(result.parts.back());result.parts.pop_back();}
            if(group.size()<fanout-1||merging.size()+group.size()>16){for(auto it=group.rbegin();it!=group.rend();++it)result.parts.push_back(*it);break;}
            for(auto &p:group){level=std::max(level,p.level);merge_weight+=p.count;merging.push_back(std::move(p));}++level;
        }
        bool native=false;Part part=merged(result,merging,payload,native);part.level=level;result.parts.push_back(part);result.count+=added;
        std::vector<Recipe> recipes;if(native&&(!in_transaction||transaction_statements==0)){Recipe r;r.output=part;r.dimension=result.dimension();r.sources=std::move(merging);r.payload=std::move(payload);
            if(auto enabled=std::getenv("MINIDB_SCALAR_SUFFIX");enabled&&std::string(enabled)=="1"){
                auto encoded=scalar_suffix_pack(r.payload,r.dimension,added);if(encoded){r.payload=std::move(*encoded);r.encoding="scalar-suffix-v1";}
            }
            recipes.push_back(std::move(r));}
        install(name,std::move(result),std::move(recipes));return added;
    }
    std::optional<std::pair<Bytes,Bytes>> lookup(const Table &table,int64_t id,size_t *position=nullptr,bool read_vectors=true)const{
        for(auto &part:table.parts){
            if(id<part.minimum||id>part.maximum||std::binary_search(part.excluded.begin(),part.excluded.end(),id))continue;
            Descriptor keys(::open((directory/part.keys).c_str(),O_RDONLY));if(keys.fd<0)::error("open primary keys");char magic[8];read_at(keys.fd,magic,8,part.key_offset);bool offsets=!std::memcmp(magic,"MVKEY002",8);if(!offsets&&std::memcmp(magic,"MVKEY001",8))throw std::runtime_error("invalid key directory");size_t stride=offsets?16:8,lo=0,hi=part.count;bool found=false;
            while(lo<hi){size_t mid=lo+(hi-lo)/2;int64_t value;read_at(keys.fd,&value,8,part.key_offset+8+mid*stride);if(value<id)lo=mid+1;else if(value>id)hi=mid;else{lo=mid;found=true;break;}}
            if(!found)continue;
            if(position)*position=size_t(&part-table.parts.data());
            if(!offsets){Table one=table;one.parts={part};Scanner scan(directory,one,read_vectors);Bytes scalar,vector;int64_t key;while(scan.next(key,scalar,vector))if(key==id)return std::make_pair(std::move(scalar),std::move(vector));throw std::runtime_error("missing primary key row");}
            uint64_t offset;read_at(keys.fd,&offset,8,part.key_offset+8+lo*16+8);Descriptor rows(::open((directory/part.file).c_str(),O_RDONLY));if(rows.fd<0)::error("open row file");uint32_t size;read_at(rows.fd,&size,4,offset);Bytes scalar(size);read_at(rows.fd,scalar.data(),scalar.size(),offset+4);Bytes vector(read_vectors?table.dimension()*4:0);
            if(table.vector_column>=0&&read_vectors){Descriptor values(::open((directory/part.vectors).c_str(),O_RDONLY));if(values.fd<0)::error("open vector file");read_at(values.fd,vector.data(),vector.size(),8+lo*(8+table.dimension()*4)+8);}
            return std::make_pair(std::move(scalar),std::move(vector));
        }return std::nullopt;
    }
    void replace_point(const std::string &name,int64_t id,size_t position,const std::optional<Row> &row){
        Table result=table(name);auto &part=result.parts.at(position);auto at=std::lower_bound(part.excluded.begin(),part.excluded.end(),id);
        if(at!=part.excluded.end()&&*at==id)throw std::runtime_error("point row already excluded");part.excluded.insert(at,id);--result.count;
        if(part.excluded.size()==part.count)result.parts.erase(result.parts.begin()+position);
        if(!row){install(name,std::move(result));return;}
        Bytes scalar=Codec{result}.scalars(*row),payload;::put(payload,id);uint32_t length=scalar.size();::put(payload,length);payload.insert(payload.end(),scalar.begin(),scalar.end());
        if(result.vector_column>=0){auto &vector=std::get<std::vector<float>>((*row)[result.vector_column]);auto first=reinterpret_cast<const unsigned char*>(vector.data());payload.insert(payload.end(),first,first+vector.size()*4);}
        insert_into(name,std::move(result),std::move(payload),{id});
    }
    template<class Transform> size_t rewrite(const std::string &name,Transform transform){
        Table result=table(name);Scanner scan(directory,result);auto writer=std::make_unique<SegmentWriter>(directory,result,fresh("rows"),fresh("index"),fresh("keys"));
        int64_t id;Bytes scalar,vector;size_t affected=0;
        while(scan.next(id,scalar,vector)){
            Row row=Codec{result}.decode(scalar,vector);int action=transform(row);if(action){++affected;if(action<0)continue;scalar=Codec{result}.scalars(row);if(result.vector_column>=0){auto &v=std::get<std::vector<float>>(row[result.vector_column]);vector.resize(v.size()*4);std::memcpy(vector.data(),v.data(),vector.size());}}
            writer->append(id,scalar,vector);
        }
        Part part=writer->finish();writer.reset();result.parts.clear();result.count=part.count;if(part.count)result.parts.push_back(std::move(part));install(name,std::move(result));return affected;
    }
};
}
