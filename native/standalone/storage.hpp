#pragma once
#include "sql.hpp"
#include "../storage.cpp"
#include <sys/random.h>
#include <sys/mman.h>
#include <zlib.h>
namespace cppdb {
using Bytes=std::vector<unsigned char>;
using Cell=std::variant<int64_t,std::string,std::vector<float>>;
using Row=std::vector<Cell>;
inline std::vector<float> vector_value(const Value &value,size_t dimension){
    std::vector<Value> numbers;
    if(value.kind==Value::Text){
        if(dimension<=4096){std::vector<float> fast(dimension);
            if(!mv_vector_parse(value.text.data(),value.text.size(),dimension,fast.data()))return fast;}
        try {
        Lexer lex(value.text,false);Token token=lex.next();if(token.text!="[")fail("invalid vector literal","invalid_vector");
        token=lex.next();if(token.text!="]")while(true){
            if(token.kind!=Token::Number)fail("invalid vector element","invalid_vector");numbers.push_back(Value::number(token.text));
            token=lex.next();if(token.text=="]")break;if(token.text!=",")fail("invalid vector separator","invalid_vector");token=lex.next();
        }
        if(lex.next().kind!=Token::End)fail("invalid vector literal","invalid_vector");
        }catch(const SqlError &){fail("invalid vector literal","invalid_vector");}
    }else if(value.kind==Value::Vector)numbers=value.elements;
    else fail("expected vector","invalid_vector");
    if(numbers.size()!=dimension)fail("vector dimension mismatch","dimension_mismatch");
    std::vector<float> out;out.reserve(dimension);
    for(auto &n:numbers){double d=n.real_value("invalid_vector");float f=static_cast<float>(d);if(!std::isfinite(f))fail("invalid float32 vector element","invalid_vector");if(value.kind!=Value::Text&&n.integral&&!d)f=0;out.push_back(f);}return out;
}
inline Cell convert(const Column &column,const Value &value,bool predicate=false){
    if(column.type=="BIGINT")return value.bigint();
    if(column.type=="VARCHAR"){
        if(value.kind!=Value::Text)fail("expected VARCHAR","invalid_value");size_t count=unicode_length(value.text);
        if(!predicate&&count>column.n)fail("VARCHAR value too long","value_too_long");return value.text;
    }
    if(predicate)fail("cannot compare VECTOR");return vector_value(value,column.n);
}
inline std::string cell_json(const Cell &value){
    if(auto p=std::get_if<int64_t>(&value))return std::to_string(*p);
    if(auto p=std::get_if<std::string>(&value))return quote(*p);
    std::string out="[";bool first=true;for(float x:std::get<std::vector<float>>(value)){if(!first)out+=',';first=false;out+=real(double(x));}out+=']';return out;
}
struct Part {
    std::string file,vectors,keys;size_t count=0,key_offset=0,level=0;int64_t minimum=0,maximum=0;bool columnar=false;
    std::vector<int64_t> excluded;
    Json json()const{
        Json out=Json::object();out["file"]=file;out["keys"]=keys;out["index_file"]=vectors.empty()?Json(nullptr):Json(vectors);
        out["count"]=count;out["key_offset"]=key_offset;out["level"]=level;out["columnar"]=columnar;
        out["min"]=count?Json(minimum):Json(nullptr);out["max"]=count?Json(maximum):Json(nullptr);
        if(!excluded.empty()){Json e=Json::array();for(auto id:excluded)e.push(id);out["excluded"]=e;}return out;
    }
    static Part from(const Json &j){
        Part p;p.file=j.at("file").string();p.keys=j.contains("keys")&&!j.at("keys").is_null()?j.at("keys").string():p.file;
        if(j.contains("index_file")&&!j.at("index_file").is_null())p.vectors=j.at("index_file").string();
        p.count=j.at("count").integer();if(p.count){p.minimum=j.at("min").integer();p.maximum=j.at("max").integer();}
        p.key_offset=j.contains("key_offset")?j.at("key_offset").integer():0;p.level=j.contains("level")?j.at("level").integer():0;
        p.columnar=j.contains("columnar")&&j.at("columnar").boolean();
        if(j.contains("excluded"))for(auto &x:j.at("excluded").items())p.excluded.push_back(x.integer());std::sort(p.excluded.begin(),p.excluded.end());return p;
    }
};
struct Table {
    std::vector<Column> columns;size_t pk=0,count=0;int vector_column=-1;std::vector<std::string> indexes;std::vector<Part> parts;
    size_t dimension()const{return vector_column<0?0:columns[vector_column].n;}
    size_t column(std::string_view name)const{auto key=fold(name);for(size_t i=0;i<columns.size();i++)if(fold(columns[i].name)==key)return i;fail("unknown column "+std::string(name),"unknown_column");}
    Json json()const{
        Json out=Json::object();Json cols=Json::array();for(auto &c:columns){Json j=Json::object();j["name"]=c.name;j["type"]=c.type;j["n"]=c.n;j["pk"]=c.primary;cols.push(j);}out["cols"]=cols;out["pk"]=pk;out["count"]=count;
        Json ix=Json::array();for(auto &s:indexes)ix.push(s);out["indexes"]=ix;Json segments=Json::array();for(auto &p:parts)segments.push(p.json());out["segments"]=segments;return out;
    }
    static Table from(const Json &j){
        Table t;t.pk=j.at("pk").integer();t.count=j.at("count").integer();for(auto &v:j.at("cols").items()){
            Column c;c.name=v.at("name").string();c.type=v.at("type").string();c.n=v.at("n").integer();c.primary=v.at("pk").boolean();if(c.type=="VECTOR")t.vector_column=t.columns.size();t.columns.push_back(c);
        }
        for(auto &v:j.at("indexes").items())t.indexes.push_back(v.string());if(j.contains("segments"))for(auto &v:j.at("segments").items())t.parts.push_back(Part::from(v));return t;
    }
};
using Catalog=std::map<std::string,Table>;
inline Json catalog_json(const Catalog &tables){Json out=Json::object();for(auto &[name,table]:tables)out[name]=table.json();return out;}
inline Catalog catalog_from(const Json &j){Catalog out;for(auto &[name,value]:j.fields())out.emplace(name,Table::from(value));return out;}
inline std::set<std::string> catalog_files(const Catalog &tables){
    std::set<std::string> names;for(auto &[_,t]:tables)for(auto &p:t.parts){names.insert(p.file);if(!p.vectors.empty())names.insert(p.vectors);if(!p.keys.empty())names.insert(p.keys);}return names;
}
struct Codec {
    const Table &table;
    Bytes scalars(const Row &row)const{
        Bytes out;for(size_t i=0;i<table.columns.size();i++){
            auto &c=table.columns[i];if(c.type=="VECTOR")continue;
            if(c.type=="BIGINT"){auto id=std::get<int64_t>(row[i]);::put(out,id);}
            else{auto &s=std::get<std::string>(row[i]);if(s.size()>UINT32_MAX)fail("VARCHAR value too large","invalid_value");uint32_t size=s.size();::put(out,size);out.insert(out.end(),s.begin(),s.end());}
        }return out;
    }
    Row decode(const Bytes &scalar,const Bytes &vector,const std::vector<bool> *wanted=nullptr)const{
        Row out(table.columns.size());size_t pos=0;
        for(size_t i=0;i<table.columns.size();i++){
            auto &c=table.columns[i];bool keep=!wanted||wanted->at(i);
            if(c.type=="BIGINT"){if(pos>scalar.size()||scalar.size()-pos<8)throw std::runtime_error("incomplete integer");if(keep){int64_t x;std::memcpy(&x,scalar.data()+pos,8);out[i]=x;}pos+=8;}
            else if(c.type=="VARCHAR"){if(pos>scalar.size()||scalar.size()-pos<4)throw std::runtime_error("incomplete text size");uint32_t n;std::memcpy(&n,scalar.data()+pos,4);pos+=4;if(n>scalar.size()-pos)throw std::runtime_error("incomplete text");if(keep)out[i]=std::string(reinterpret_cast<const char*>(scalar.data()+pos),n);pos+=n;}
            else if(keep){if(c.n>SIZE_MAX/4||vector.size()!=c.n*4)throw std::runtime_error("invalid vector stream");std::vector<float> v(c.n);std::memcpy(v.data(),vector.data(),vector.size());out[i]=std::move(v);}
        }
        if(pos!=scalar.size())throw std::runtime_error("extra scalar bytes");return out;
    }
    int64_t primary(const Bytes &scalar)const{
        size_t pos=0;for(size_t i=0;i<table.columns.size();i++){
            auto &c=table.columns[i];if(c.type=="VECTOR")continue;
            if(c.type=="BIGINT"){if(pos>scalar.size()||scalar.size()-pos<8)throw std::runtime_error("incomplete scalar integer");if(i==table.pk){int64_t id;std::memcpy(&id,scalar.data()+pos,8);return id;}pos+=8;}
            else{if(pos>scalar.size()||scalar.size()-pos<4)throw std::runtime_error("incomplete scalar text");uint32_t n;std::memcpy(&n,scalar.data()+pos,4);pos+=4;if(n>scalar.size()-pos)throw std::runtime_error("incomplete scalar text");pos+=n;}
        }throw std::runtime_error("missing primary key");
    }
};
class Scanner {
    struct Item {int64_t id;size_t source;};
    struct Compare {bool reverse=false;bool operator()(const Item &a,const Item &b)const{return reverse?a.id<b.id:a.id>b.id;}};
    const Table &table;bool read_vectors,backwards;std::vector<std::unique_ptr<::Cursor>> cursors;std::vector<mv_source> sources;
    std::priority_queue<Item,std::vector<Item>,Compare> heap;
    std::vector<std::unique_ptr<::File>> scalar_files;std::vector<size_t> left;
    std::vector<std::unique_ptr<::File>> key_files;std::vector<size_t> right,key_first;std::vector<Bytes> key_blocks;
    void advance(size_t index){
        if(backwards){
            auto &part=table.parts[index];auto &cursor=*cursors[index];auto &keys=*key_files[index];auto &block=key_blocks[index];
            while(left[index]){
                --left[index];size_t row=--right[index];
                if(block.empty()||row<key_first[index]||row-key_first[index]>=block.size()/16){
                    size_t first=row/256*256,count=std::min(size_t(256),part.count-first);block.resize(count*16);key_first[index]=first;
                    if(::fseeko(keys.f,part.key_offset+8+first*16,SEEK_SET))throw std::runtime_error("reverse directory seek failed");keys.read(block.data(),block.size());
                }
                int64_t id;uint64_t offset;auto ptr=block.data()+(row-key_first[index])*16;std::memcpy(&id,ptr,8);std::memcpy(&offset,ptr+8,8);
                if(std::binary_search(part.excluded.begin(),part.excluded.end(),id))continue;
                if(::fseeko(cursor.rows->f,offset,SEEK_SET))throw std::runtime_error("reverse row seek failed");uint32_t size;cursor.rows->read(&size,4);cursor.scalar.resize(size);cursor.rows->read(cursor.scalar.data(),size);
                if(read_vectors){if(::fseeko(cursor.vectors->f,8+row*(8+table.dimension()*4)+8,SEEK_SET))throw std::runtime_error("reverse vector seek failed");cursor.vectors->read(cursor.vector.data(),cursor.vector.size());}
                cursor.id=id;heap.push({id,index});return;
            }return;
        }
        if(read_vectors){if(cursors[index]->next())heap.push({cursors[index]->id,index});return;}
        auto &part=table.parts[index];auto &file=*scalar_files[index];
        while(left[index]){
            uint32_t size;file.read(&size,4);Bytes raw(size);file.read(raw.data(),raw.size());--left[index];
            // Scalar-only primary-key position is obtained from its typed codec.
            int64_t id=Codec{table}.primary(raw);
            if(std::binary_search(part.excluded.begin(),part.excluded.end(),id))continue;
            auto &c=*cursors[index];c.id=id;c.scalar=std::move(raw);heap.push({id,index});return;
        }
    }
public:
    Scanner(const std::filesystem::path &root,const Table &t,bool include_vectors=true,std::optional<std::pair<int64_t,int64_t>> bounds={},bool reverse=false):table(t),read_vectors(t.vector_column>=0&&include_vectors),backwards(reverse),heap(Compare{reverse}){
        sources.resize(t.parts.size());left.resize(t.parts.size());scalar_files.resize(t.parts.size());cursors.reserve(t.parts.size());
        if(backwards){key_files.resize(t.parts.size());right.resize(t.parts.size());key_first.resize(t.parts.size());key_blocks.resize(t.parts.size());}
        for(size_t i=0;i<t.parts.size();i++){
            auto &p=t.parts[i];auto c=std::make_unique<::Cursor>(read_vectors?t.dimension()*4:0);size_t first=0,last=p.count;uint64_t row_start=8;
            if(bounds){
                if(p.maximum<bounds->first||p.minimum>bounds->second)last=0;
                else if(bounds->first>p.minimum||bounds->second<p.maximum){
                    int fd=::open((root/p.keys).c_str(),O_RDONLY);if(fd<0)::error("open range directory");
                    struct Closer{int fd;~Closer(){::close(fd);}} owner{fd};
                    auto read=[&](void *out,size_t bytes,uint64_t at){size_t n=0;while(n<bytes){auto got=::pread(fd,static_cast<unsigned char*>(out)+n,bytes-n,at+n);if(got<0&&errno==EINTR)continue;if(got<=0)throw std::runtime_error("incomplete range directory");n+=got;}};
                    char magic[8];read(magic,8,p.key_offset);bool offsets=!std::memcmp(magic,"MVKEY002",8);if(!offsets&&std::memcmp(magic,"MVKEY001",8))throw std::runtime_error("invalid range directory");size_t stride=offsets?16:8;
                    auto bound=[&](int64_t target,bool upper){size_t lo=0,hi=p.count;while(lo<hi){size_t mid=lo+(hi-lo)/2;int64_t key;read(&key,8,p.key_offset+8+mid*stride);if(key<target||(upper&&key==target))lo=mid+1;else hi=mid;}return lo;};
                    first=bound(bounds->first,false);last=bound(bounds->second,true);
                    if(!offsets)first=0;else if(first<last)read(&row_start,8,p.key_offset+8+first*16+8);
                }
            }
            if(backwards){
                left[i]=last-first;right[i]=last;c->rows=std::make_unique<::File>((root/p.file).c_str(),"rb");key_files[i]=std::make_unique<::File>((root/p.keys).c_str(),"rb");
                char magic[8];c->rows->read(magic,8);if(std::memcmp(magic,"MVROW001",8))throw std::runtime_error("invalid reverse row header");
                if(::fseeko(key_files[i]->f,p.key_offset,SEEK_SET))throw std::runtime_error("reverse key header seek failed");key_files[i]->read(magic,8);if(std::memcmp(magic,"MVKEY002",8))throw std::runtime_error("reverse scan requires row offsets");
                if(read_vectors){c->vectors=std::make_unique<::File>((root/p.vectors).c_str(),"rb");c->vectors->read(magic,8);if(std::memcmp(magic,"MVFLT001",8))throw std::runtime_error("invalid reverse vector header");}
            }else if(read_vectors){
                sources[i]={nullptr,nullptr,p.count,p.excluded.data(),p.excluded.size()};c->source=&sources[i];c->left=last-first;
                c->rows=std::make_unique<::File>((root/p.file).c_str(),"rb");c->vectors=std::make_unique<::File>((root/p.vectors).c_str(),"rb");char magic[8];c->rows->read(magic,8);if(std::memcmp(magic,"MVROW001",8))throw std::runtime_error("invalid row header");c->vectors->read(magic,8);if(std::memcmp(magic,"MVFLT001",8))throw std::runtime_error("invalid vector header");
                if(first<last&&(::fseeko(c->rows->f,row_start,SEEK_SET)||::fseeko(c->vectors->f,8+first*(8+t.dimension()*4),SEEK_SET)))throw std::runtime_error("range seek failed");
            }else{scalar_files[i]=std::make_unique<::File>((root/p.file).c_str(),"rb");char magic[8];scalar_files[i]->read(magic,8);if(std::memcmp(magic,"MVROW001",8))throw std::runtime_error("invalid row header");left[i]=last-first;if(first<last&&::fseeko(scalar_files[i]->f,row_start,SEEK_SET))throw std::runtime_error("scalar range seek failed");}
            cursors.push_back(std::move(c));
        }
        for(size_t i=0;i<cursors.size();i++)advance(i);
    }
    bool next(int64_t &id,Bytes &scalar,Bytes &vector){
        if(heap.empty())return false;auto item=heap.top();heap.pop();auto &c=*cursors[item.source];id=c.id;scalar=c.scalar;vector=c.vector;advance(item.source);return true;
    }
};
class SegmentWriter {
    std::unique_ptr<::File> rows,vectors,keys;std::filesystem::path key_path;const Table &table;Part part;
public:
    SegmentWriter(const std::filesystem::path &root,const Table &t,std::string row_name,std::string vector_name,std::string key_name):key_path(root/key_name),table(t){
        part.file=row_name;part.keys=row_name;part.vectors=t.vector_column<0?"":vector_name;part.columnar=t.vector_column>=0;
        rows=std::make_unique<::File>((root/row_name).c_str(),"wbx");rows->write("MVROW001",8);
        keys=std::make_unique<::File>(key_path.c_str(),"w+bx");keys->write("MVKEY002",8);
        if(t.vector_column>=0){vectors=std::make_unique<::File>((root/vector_name).c_str(),"wbx");vectors->write("MVFLT001",8);}
    }
    void append(int64_t id,const Bytes &scalar,const Bytes &vector){
        if(part.count&&id<=part.maximum)throw std::runtime_error("unordered output segment");
        if(scalar.size()>UINT32_MAX)throw std::runtime_error("row too large");uint32_t size=scalar.size();uint64_t offset=rows->tell();rows->write(&size,4);rows->write(scalar.data(),scalar.size());keys->write(&id,8);keys->write(&offset,8);
        if(vectors){if(vector.size()!=table.dimension()*4)throw std::runtime_error("invalid vector size");vectors->write(&id,8);vectors->write(vector.data(),vector.size());}
        if(!part.count)part.minimum=id;part.maximum=id;++part.count;
    }
    Part finish(){
        part.key_offset=rows->tell();if(std::fflush(keys->f)||::fseeko(keys->f,0,SEEK_SET))throw std::runtime_error("directory seek failed");
        unsigned char block[65536];size_t n;while((n=std::fread(block,1,sizeof(block),keys->f)))rows->write(block,n);if(std::ferror(keys->f))throw std::runtime_error("directory read failed");keys->close();if(::unlink(key_path.c_str()))throw std::runtime_error("directory temporary unlink failed");rows->sync();if(vectors)vectors->sync();return part;
    }
};
}
