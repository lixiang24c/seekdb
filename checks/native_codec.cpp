#include "../native/standalone/recipe_codec.hpp"
#include <cassert>
#include <random>
using namespace cppdb;
int main(){
    std::mt19937 rng(872909);size_t packed=0,unchanged=0;
    for(size_t trial=0;trial<1200;trial++){
        size_t count=4+rng()%80,dimension=2+rng()%16;Bytes input;
        for(size_t i=0;i<count;i++){
            int64_t id=int64_t(i)-40;Bytes scalar;::put(scalar,id);
            std::string text(512,'x');if(trial%2)for(char &c:text)c=char(32+rng()%90);
            ::put(scalar,uint32_t(text.size()));scalar.insert(scalar.end(),text.begin(),text.end());
            ::put(input,id);::put(input,uint32_t(scalar.size()));input.insert(input.end(),scalar.begin(),scalar.end());
            for(size_t d=0;d<dimension;d++){float value=d%2?-0.0f:float(i+d);::put(input,value);}
        }
        auto encoded=scalar_suffix_pack(input,dimension,count);
        if(encoded){
            ++packed;assert(scalar_suffix_unpack(*encoded,dimension)==input);
            encoded->pop_back();bool rejected=false;try{scalar_suffix_unpack(*encoded,dimension);}catch(const std::runtime_error&){rejected=true;}assert(rejected);
        }else ++unchanged;
    }
    for(int trial=0;trial<16;trial++){
        std::string text=trial%2?"VALUES(1,'hé''界','[-0,2]')":"VALUES(1,'hé界','[-0,2]')";
        mv_column cols[]={{"id",0,0},{"body",1,128},{"v",2,2}};
        std::unique_ptr<Batch,void(*)(void*)> batch(static_cast<Batch*>(mv_insert_prepare(text.data(),text.size(),cols,3,0)),mv_insert_free);assert(batch);
        std::fill(text.begin(),text.end(),'!');std::string().swap(text);
        assert(batch->ids==std::vector<int64_t>{1});uint32_t n;std::memcpy(&n,batch->data.data()+8,4);
        Table table;table.columns={{"id","BIGINT",0,true},{"body","VARCHAR",128,false},{"v","VECTOR",2,false}};table.pk=0;table.vector_column=2;
        Bytes scalar(batch->data.begin()+12,batch->data.begin()+12+n),vector(batch->data.begin()+12+n,batch->data.end());
        Row row=Codec{table}.decode(scalar,vector);assert(std::get<std::string>(row[1])==(trial%2?"hé'界":"hé界"));
        auto &v=std::get<std::vector<float>>(row[2]);assert(std::signbit(v[0])&&v[1]==2);
    }
    std::cout<<"{\"status\":\"PASS\",\"codec_roundtrips\":"<<packed<<",\"uncompressed\":"<<unchanged<<",\"literal_ownership\":16}\n";
}
