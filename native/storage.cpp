// Native physical-column merge and durable segment construction.
#include <algorithm>
#include <cerrno>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <memory>
#include <queue>
#include <stdexcept>
#include <string>
#include <vector>
#include <unistd.h>
#include <fcntl.h>
#include <sys/stat.h>

extern "C" {
struct mv_source { const char *rows; const char *vectors; uint64_t count; const int64_t *excluded; size_t excluded_count; };
struct mv_segment { uint64_t count; uint64_t key_offset; int64_t minimum; int64_t maximum; };
}
static bool durable_segments=true;
extern "C" void mv_storage_set_durable(int value) noexcept {durable_segments=value!=0;}
namespace {
[[noreturn]] void error(const char *s) { throw std::runtime_error(std::string(s)+": "+std::strerror(errno)); }
struct File {
    FILE *f=nullptr;
    std::vector<char> buffer;
    File(const char *path,const char *mode,size_t size=8192):buffer(size) {
        f=std::fopen(path,mode);if(!f)error("open segment");
        if(std::setvbuf(f,buffer.data(),_IOFBF,buffer.size())) { std::fclose(f);f=nullptr;throw std::runtime_error("set column buffer"); }
    }
    ~File(){if(f)std::fclose(f);}
    void read(void *p,size_t n){if(n && ::fread_unlocked(p,1,n,f)!=n)throw std::runtime_error("incomplete column stream");}
    void write(const void *p,size_t n){if(n && ::fwrite_unlocked(p,1,n,f)!=n)error("write segment");}
    void close(){if(f){FILE *old=f;f=nullptr;if(std::fclose(old))error("close segment");}}
    void sync(){if(std::fflush(f)||(durable_segments&&::fsync(::fileno(f))))error("sync segment");}
    uint64_t tell(){auto n=::ftello(f);if(n<0)error("tell segment");return static_cast<uint64_t>(n);}
};
struct Cursor {
    std::unique_ptr<File> rows,vectors;
    const mv_source *source=nullptr;
    const unsigned char *input=nullptr;size_t remaining=0;
    uint64_t left=0;size_t width;
    int64_t id=0,previous=0;bool seen=false;
    std::vector<unsigned char> scalar,vector;
    Cursor(size_t w):width(w),vector(w){}
    bool next(){
        for(;;){
            uint32_t size=0;
            if(source){
                if(!left)return false;--left;
                rows->read(&size,4);scalar.resize(size);rows->read(scalar.data(),size);
                vectors->read(&id,8);vectors->read(vector.data(),width);
            }else{
                if(!remaining)return false;
                if(remaining<12)throw std::runtime_error("incomplete input row");
                std::memcpy(&id,input,8);std::memcpy(&size,input+8,4);input+=12;remaining-=12;
                if(size>remaining || width>remaining-size)throw std::runtime_error("incomplete input columns");
                scalar.assign(input,input+size);input+=size;remaining-=size;
                std::memcpy(vector.data(),input,width);input+=width;remaining-=width;
            }
            if(seen&&id<=previous)throw std::runtime_error("unsorted source column");previous=id;seen=true;
            if(source&&source->excluded_count&&std::binary_search(source->excluded,source->excluded+source->excluded_count,id))continue;
            return true;
        }
    }
};
struct Greater {bool operator()(const Cursor *a,const Cursor *b)const{return a->id>b->id;}};
}
extern "C" int mv_storage_merge(const char *row_path,const char *vector_path,const char *key_path,
 const mv_source *sources,size_t count,const unsigned char *input,size_t bytes,size_t dim,
 int bounded,mv_segment *result,char *message,size_t capacity) noexcept {
    bool made_rows=false,made_vectors=false,made_keys=false;
    try{
        if(!result||!dim||dim>4096||count>16||(!input&&bytes))throw std::runtime_error("invalid merge arguments");
        std::vector<std::unique_ptr<Cursor>> cursors;
        for(size_t i=0;i<count;++i){
            auto c=std::make_unique<Cursor>(dim*4);c->source=&sources[i];c->left=sources[i].count;
            c->rows=std::make_unique<File>(sources[i].rows,"rb",bounded?32768:8192);c->vectors=std::make_unique<File>(sources[i].vectors,"rb",bounded?32768:8192);
            char header[8];c->rows->read(header,8);if(std::memcmp(header,"MVROW001",8))throw std::runtime_error("invalid scalar header");
            c->vectors->read(header,8);if(std::memcmp(header,"MVFLT001",8))throw std::runtime_error("invalid vector header");
            if(bounded){::posix_fadvise(::fileno(c->rows->f),0,0,POSIX_FADV_RANDOM);::posix_fadvise(::fileno(c->vectors->f),0,0,POSIX_FADV_RANDOM);}
            cursors.push_back(std::move(c));
        }
        auto incoming=std::make_unique<Cursor>(dim*4);incoming->input=input;incoming->remaining=bytes;cursors.push_back(std::move(incoming));
        std::priority_queue<Cursor*,std::vector<Cursor*>,Greater> heap;
        for(auto &c:cursors)if(c->next())heap.push(c.get());
        File rows(row_path,"wbx");made_rows=true;
        File vectors(vector_path,"wbx");made_vectors=true;
        File keys(key_path,"w+bx");made_keys=true;
        rows.write("MVROW001",8);vectors.write("MVFLT001",8);keys.write("MVKEY002",8);
        mv_segment out{}; uint64_t row_offset=8;
        while(!heap.empty()){
            Cursor *c=heap.top();heap.pop();
            if(out.count&&c->id<=out.maximum)throw std::runtime_error("duplicate merged primary key");
            uint64_t offset=row_offset;uint32_t size=static_cast<uint32_t>(c->scalar.size());
            if(c->scalar.size()!=size)throw std::runtime_error("scalar row too large");
            rows.write(&size,4);rows.write(c->scalar.data(),size);row_offset+=4+uint64_t(size);
            keys.write(&c->id,8);keys.write(&offset,8);
            vectors.write(&c->id,8);vectors.write(c->vector.data(),c->vector.size());
            if(!out.count)out.minimum=c->id;out.maximum=c->id;++out.count;
            if(c->next())heap.push(c);
        }
        out.key_offset=row_offset;
        if(std::fflush(keys.f)||::fseeko(keys.f,0,SEEK_SET))error("rewind directory");
        unsigned char block[65536];size_t n;
        while((n=std::fread(block,1,sizeof(block),keys.f)))rows.write(block,n);
        if(std::ferror(keys.f))error("read directory");
        keys.close();
        if(::unlink(key_path))error("unlink directory temporary");made_keys=false;
        rows.sync();vectors.sync();
        *result=out;return 0;
    }catch(const std::exception &e){if(message&&capacity)std::snprintf(message,capacity,"%s",e.what());}
     catch(...){if(message&&capacity)std::snprintf(message,capacity,"native merge failed");}
    if(made_rows)::unlink(row_path);if(made_vectors)::unlink(vector_path);if(made_keys)::unlink(key_path);
    return -1;
}

#include <charconv>
#include <cmath>
#include <cfloat>
// Fast successful ASCII literals only. Any uncertain syntax, dimension, or
// conversion returns 1 so Python preserves its established error precedence.
extern "C" int mv_vector_parse(const char *text,size_t size,size_t dim,float *out) noexcept {
    if(!text||!out||!dim||dim>4096||size>65536)return 1;
    const char *p=text,*end=text+size;
    auto space=[&](){while(p<end&&(*p==' '||*p=='\t'||*p=='\r'||*p=='\n'||*p=='\f'||*p=='\v'))++p;};
    auto digit=[](char c){return c>='0'&&c<='9';};
    space();if(p==end||*p++!='[')return 1;
    for(size_t i=0;i<dim;++i){
        space();if(p==end)return 1;const char *first=p;
        if(*p=='+'||*p=='-')++p;
        size_t digits=0;while(p<end&&digit(*p)){++p;++digits;}
        if(p<end&&*p=='.'){++p;while(p<end&&digit(*p)){++p;++digits;}}
        if(!digits)return 1;
        if(p<end&&(*p=='e'||*p=='E')){
            ++p;if(p<end&&(*p=='+'||*p=='-'))++p;
            const char *exp=p;while(p<end&&digit(*p))++p;if(p==exp)return 1;
        }
        double value=0;
        const char *number=first+(*first=='+');
        auto result=std::from_chars(number,p,value,std::chars_format::general);
        if(result.ec!=std::errc()||result.ptr!=p||!std::isfinite(value)||std::fabs(value)>FLT_MAX)return 1;
        float converted=static_cast<float>(value);
        if(!std::isfinite(converted))return 1;
        out[i]=converted;
        space();if(p==end||*p++!=(i+1==dim?']':','))return 1;
    }
    space();return p==end?0:1;
}

extern "C" { struct mv_column { const char *name; uint32_t kind; uint32_t width; }; }
namespace {
struct Batch { std::vector<unsigned char> data; std::vector<int64_t> ids; };
struct InputRow { int64_t id=0;std::vector<unsigned char> scalar,vector; };
struct InsertParser {
    const char *p,*end;
    void space(){while(p<end&&(*p==' '||*p=='\t'||*p=='\n'||*p=='\r'||*p=='\f'||*p=='\v'))++p;}
    bool take(char c){space();if(p<end&&*p==c){++p;return true;}return false;}
    void need(char c){if(!take(c))throw std::runtime_error("reference syntax");}
    static bool letter(char c){return(c>='a'&&c<='z')||(c>='A'&&c<='Z')||c=='_';}
    static char lower(char c){return c>='A'&&c<='Z'?c+('a'-'A'):c;}
    std::string identifier(){
        space();std::string result;
        if(take('`')){while(p<end){char c=*p++;if(c=='`'){if(p<end&&*p=='`')++p;else return result;}result+=lower(c);}throw std::runtime_error("quoted identifier");}
        if(p==end||!letter(*p))throw std::runtime_error("identifier");
        while(p<end&&(letter(*p)||(*p>='0'&&*p<='9')))result+=lower(*p++);
        return result;
    }
    std::string string(){
        need('\'');std::string result;
        while(p<end){char c=*p++;if(c=='\''){if(p<end&&*p=='\'')++p;else return result;}result+=c;}
        throw std::runtime_error("quoted value");
    }
    bool plain_string(const char *&value,size_t &length){
        space();if(p==end||*p!='\'')return false;
        const char *first=p+1,*quote=static_cast<const char*>(std::memchr(first,'\'',end-first));
        if(!quote||(quote+1<end&&quote[1]=='\''))return false;
        value=first;length=quote-first;p=quote+1;return true;
    }
};
template<class T> void put(std::vector<unsigned char>& data,const T &value){const auto *p=reinterpret_cast<const unsigned char*>(&value);data.insert(data.end(),p,p+sizeof(T));}
}
// Entire statement is validated and staged before the caller can write any file.
// Unsupported/error cases return null and defer to the reference SQL executor.
extern "C" void *mv_insert_prepare(const char *text,size_t size,const mv_column *cols,size_t count,size_t pk) noexcept {
    try{
        if(!text||!cols||!count||count>256||pk>=count)return nullptr;
        InsertParser in{text,text+size};std::vector<size_t> order;
        if(in.take('(')){
            do{
                std::string name=in.identifier();size_t index=count;
                for(size_t c=0;c<count;c++){
                    std::string expected=cols[c].name;for(char &ch:expected)ch=InsertParser::lower(ch);
                    if(name==expected){index=c;break;}
                }
                if(index==count||std::find(order.begin(),order.end(),index)!=order.end())return nullptr;
                order.push_back(index);
            }while(in.take(','));in.need(')');
        }else for(size_t c=0;c<count;c++)order.push_back(c);
        if(order.size()!=count||in.identifier()!="values")return nullptr;
        std::vector<InputRow> rows;
        do{
            in.need('(');std::vector<std::vector<unsigned char>> fields(count);InputRow row;
            for(size_t pos=0;pos<count;pos++){
                if(pos)in.need(',');size_t c=order[pos];in.space();
                if(cols[c].kind==0){
                    const char *start=in.p;if(in.p<in.end&&(*in.p=='+'||*in.p=='-'))++in.p;
                    const char *digits=in.p;while(in.p<in.end&&*in.p>='0'&&*in.p<='9')++in.p;
                    if(in.p==digits)return nullptr;int64_t value=0;
                    auto parsed=std::from_chars(start+(*start=='+'),in.p,value);
                    if(parsed.ec!=std::errc()||parsed.ptr!=in.p)return nullptr;
                    put(fields[c],value);if(c==pk)row.id=value;
                }else if(cols[c].kind==1){
                    std::string decoded;const char *value=nullptr;size_t length=0,chars=0;
                    if(!in.plain_string(value,length)){decoded=in.string();value=decoded.data();length=decoded.size();}
                    for(size_t i=0;i<length;i++)if((static_cast<unsigned char>(value[i])&0xc0)!=0x80)++chars;
                    if(chars>cols[c].width||length>UINT32_MAX)return nullptr;
                    put(fields[c],static_cast<uint32_t>(length));fields[c].insert(fields[c].end(),value,value+length);
                }else if(cols[c].kind==2){
                    if(!cols[c].width||cols[c].width>4096)return nullptr;
                    std::string quoted;const char *v=nullptr;size_t length=0;
                    bool is_quoted=in.p<in.end&&*in.p=='\'';
                    if(is_quoted){if(!in.plain_string(v,length)){quoted=in.string();v=quoted.data();length=quoted.size();}}
                    else{
                        const char *start=in.p;in.need('[');while(in.p<in.end&&*in.p!=']')++in.p;
                        if(in.p==in.end)return nullptr;++in.p;v=start;length=in.p-start;
                    }
                    std::vector<float> values(cols[c].width);
                    if(mv_vector_parse(v,length,values.size(),values.data()))return nullptr;
                    // Bare SQL integer tokens pass through Python int in the
                    // reference parser: -0 becomes +0. Quoted vector text and
                    // decimal/exponent tokens preserve their negative zero.
                    if(!is_quoted){
                        const char *token=v+1;
                        for(size_t i=0;i<values.size();++i){
                            const char *stop=token;bool integer=true;
                            while(stop<v+length&&*stop!=','&&*stop!=']'){
                                if(*stop=='.'||*stop=='e'||*stop=='E')integer=false;
                                ++stop;
                            }
                            if(integer&&values[i]==0)values[i]=0.0f;
                            token=stop+1;
                        }
                    }
                    auto first=reinterpret_cast<const unsigned char*>(values.data());fields[c].assign(first,first+values.size()*sizeof(float));
                }else return nullptr;
            }
            in.need(')');
            for(size_t c=0;c<count;c++){
                auto &target=cols[c].kind==2?row.vector:row.scalar;
                target.insert(target.end(),fields[c].begin(),fields[c].end());
            }
            if(row.scalar.size()>UINT32_MAX)return nullptr;
            rows.push_back(std::move(row));
        }while(in.take(','));
        in.take(';');in.space();if(in.p!=in.end||rows.empty())return nullptr;
        std::sort(rows.begin(),rows.end(),[](const InputRow&a,const InputRow&b){return a.id<b.id;});
        auto batch=std::make_unique<Batch>();
        for(auto &row:rows){
            if(!batch->ids.empty()&&row.id==batch->ids.back())return nullptr;
            batch->ids.push_back(row.id);put(batch->data,row.id);put(batch->data,static_cast<uint32_t>(row.scalar.size()));
            batch->data.insert(batch->data.end(),row.scalar.begin(),row.scalar.end());batch->data.insert(batch->data.end(),row.vector.begin(),row.vector.end());
        }
        return batch.release();
    }catch(...){return nullptr;}
}
extern "C" void mv_insert_free(void *ptr) noexcept {delete static_cast<Batch*>(ptr);}
extern "C" size_t mv_insert_count(void *ptr) noexcept {return ptr?static_cast<Batch*>(ptr)->ids.size():0;}
extern "C" const unsigned char *mv_insert_data(void *ptr,size_t *size) noexcept {
    if(!ptr||!size)return nullptr;auto &data=static_cast<Batch*>(ptr)->data;*size=data.size();return data.data();
}
extern "C" int mv_insert_ids(void *ptr,int64_t *out,size_t count) noexcept {
    if(!ptr||!out||static_cast<Batch*>(ptr)->ids.size()!=count)return -1;
    const auto &ids=static_cast<Batch*>(ptr)->ids;std::copy(ids.begin(),ids.end(),out);return 0;
}

// Positive binary64 products are accumulated exactly as integers in units of
// 2^-1074, then rounded once to nearest, ties to even. This preserves fsum's
// result without a Python float object or a per-candidate FFI call.
namespace {
struct PositiveSum {
    uint64_t limbs[34]{};
    void add(double value){
        uint64_t bits;std::memcpy(&bits,&value,8);
        unsigned exponent=unsigned((bits>>52)&2047);
        uint64_t mantissa=bits&((uint64_t(1)<<52)-1);
        if(exponent==2047||bits>>63)throw std::runtime_error("invalid squared term");
        if(exponent)mantissa|=uint64_t(1)<<52;
        if(!mantissa)return;
        unsigned shift=exponent?exponent-1:0,index=shift/64,offset=shift%64;
        uint64_t low=mantissa<<offset,high=offset?mantissa>>(64-offset):0;
        uint64_t old=limbs[index];limbs[index]+=low;uint64_t carry=limbs[index]<old;
        ++index;old=limbs[index];limbs[index]+=high;uint64_t next=limbs[index]<old;
        old=limbs[index];limbs[index]+=carry;carry=next|(limbs[index]<old);
        while(carry&&++index<34){old=limbs[index];++limbs[index];carry=limbs[index]<old;}
        if(carry)throw std::runtime_error("distance accumulation overflow");
    }
    double result()const{
        int word=33;while(word>=0&&!limbs[word])--word;if(word<0)return 0;
        unsigned top=unsigned(word)*64+63-unsigned(__builtin_clzll(limbs[word]));
        uint64_t encoded=0;
        if(top<52)encoded=limbs[0];
        else{
            unsigned shift=top-52,index=shift/64,offset=shift%64;
            uint64_t significand=limbs[index]>>offset;
            if(offset&&index+1<34)significand|=limbs[index+1]<<(64-offset);
            significand&=(uint64_t(1)<<53)-1;
            if(shift){
                unsigned half=shift-1;bool halfway=(limbs[half/64]>>(half%64))&1;
                bool sticky=false;
                for(unsigned j=0;j<half/64;j++)sticky|=limbs[j]!=0;
                if(half%64)sticky|=(limbs[half/64]&((uint64_t(1)<<(half%64))-1))!=0;
                if(halfway&&(sticky||(significand&1)))++significand;
            }
            if(significand==(uint64_t(1)<<53)){significand>>=1;++top;}
            unsigned exponent=top-51;if(exponent>=2047)throw std::runtime_error("distance overflow");
            encoded=(uint64_t(exponent)<<52)|(significand&((uint64_t(1)<<52)-1));
        }
        double out;std::memcpy(&out,&encoded,8);return out;
    }
};
struct TopK {
    std::vector<double> query;
    size_t k;
    std::vector<std::pair<double,int64_t>> heap;
    std::vector<uint32_t> positions;
    TopK(const double *q,size_t d,size_t count):query(q,q+d),k(count){heap.reserve(k);}
    double bound()const{return heap.size()==k?heap.front().first:INFINITY;}
    void offer(const unsigned char *record){
        int64_t id;std::memcpy(&id,record,8);PositiveSum sum;
        for(size_t d=0;d<query.size();++d){
            float value;std::memcpy(&value,record+8+d*4,4);
            double delta=double(value)-query[d];sum.add(delta*delta);
        }
        auto item=std::make_pair(sum.result(),id);
        if(heap.size()<k){heap.push_back(item);std::push_heap(heap.begin(),heap.end());}
        else if(item<heap.front()){std::pop_heap(heap.begin(),heap.end());heap.back()=item;std::push_heap(heap.begin(),heap.end());}
    }
    void offer_score(double score,int64_t id){
        auto item=std::make_pair(score,id);
        if(heap.size()<k){heap.push_back(item);std::push_heap(heap.begin(),heap.end());}
        else if(item<heap.front()){std::pop_heap(heap.begin(),heap.end());heap.back()=item;std::push_heap(heap.begin(),heap.end());}
    }
};
}
extern "C" size_t mv_screen_topk(const unsigned char*,size_t,size_t,const double*,double,const unsigned char*,size_t,uint32_t*);
extern "C" void *mv_topk_new(const double *query,size_t dim,size_t k) noexcept{
    try{if(!query||!dim||dim>4096||!k||k>1024)return nullptr;
        for(size_t i=0;i<dim;i++)if(!std::isfinite(query[i]))return nullptr;
        return new TopK(query,dim,k);
    }catch(...){return nullptr;}
}
extern "C" void mv_topk_free(void *p) noexcept{delete static_cast<TopK*>(p);}
extern "C" int mv_topk_scan(void *p,const unsigned char *data,size_t bytes,const unsigned char *mask) noexcept{
    try{
        auto *ctx=static_cast<TopK*>(p);if(!ctx||!data)return -1;
        size_t stride=8+ctx->query.size()*4;if(bytes%stride)return -1;
        ctx->positions.resize(bytes/stride);
        auto &positions=ctx->positions;
        size_t n=mv_screen_topk(data,bytes,ctx->query.size(),ctx->query.data(),ctx->bound(),mask,ctx->k,positions.data());
        if(n==SIZE_MAX||n>positions.size())return -1;
        for(size_t i=0;i<n;i++)ctx->offer(data+positions[i]*stride);
        return 0;
    }catch(...){return -1;}
}
extern "C" int mv_topk_file(void *p,const char *path,const uint32_t *positions,size_t count,uint64_t *read_bytes) noexcept{
    try{
        auto *ctx=static_cast<TopK*>(p);if(!ctx||!path||(!positions&&count))return -1;
        File file(path,"rb");::posix_fadvise(::fileno(file.f),0,0,POSIX_FADV_RANDOM);
        char magic[8];
        if(::pread(::fileno(file.f),magic,8,0)!=8||std::memcmp(magic,"MVFLT001",8))return -1;
        struct stat st;if(::fstat(::fileno(file.f),&st)||st.st_size<8)return -1;
        size_t stride=8+ctx->query.size()*4;uint64_t total=0;
        for(size_t first=0;first<count;){
            if(first&&positions[first]<=positions[first-1])return -1;
            uint64_t offset=8+uint64_t(positions[first])*stride;
            uint64_t start=offset/4096*4096,stop=(offset+stride+4095)/4096*4096;
            size_t last=first+1;
            while(last<count){
                if(positions[last]<=positions[last-1])return -1;
                offset=8+uint64_t(positions[last])*stride;if(offset/4096*4096>stop)break;
                stop=std::max(stop,(offset+stride+4095)/4096*4096);++last;
            }
            stop=std::min(stop,uint64_t(st.st_size));if(stop<=start)return -1;
            std::vector<unsigned char> block(stop-start);size_t done=0;
            while(done<block.size()){
                auto got=::pread(::fileno(file.f),block.data()+done,block.size()-done,start+done);
                if(got<=0)return -1;done+=size_t(got);
            }
            total+=block.size();
            for(size_t i=first;i<last;i++){
                offset=8+uint64_t(positions[i])*stride-start;
                if(offset>block.size()||stride>block.size()-offset)return -1;
                ctx->offer(block.data()+offset);
            }
            first=last;
        }
        if(read_bytes)*read_bytes=total;return 0;
    }catch(...){return -1;}
}
extern "C" size_t mv_topk_result(void *p,int64_t *ids,double *scores,size_t capacity) noexcept{
    try{
        auto *ctx=static_cast<TopK*>(p);if(!ctx||!ids||!scores||capacity<ctx->heap.size())return SIZE_MAX;
        auto rows=ctx->heap;std::sort(rows.begin(),rows.end());
        for(size_t i=0;i<rows.size();i++){scores[i]=rows[i].first;ids[i]=rows[i].second;}return rows.size();
    }catch(...){return SIZE_MAX;}
}
