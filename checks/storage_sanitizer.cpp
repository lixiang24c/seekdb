// Test-only sanitizer harness; the implementation is compiled with matching FP flags.
#include "../native/storage.cpp"
#include <cassert>
#include <random>
#include <filesystem>
int main(int argc,char **argv){
    assert(argc==2);std::string root=argv[1];
    PositiveSum sum;sum.add(1.);sum.add(0x1p-53);assert(sum.result()==1.);
    sum.add(0x1p-54);assert(sum.result()==std::nextafter(1.,2.));
    PositiveSum sub;sub.add(0x1p-1074);sub.add(0x1p-1074);assert(sub.result()==0x1p-1073);
    mv_column cols[]={{"id",0,0},{"s",1,16},{"v",2,16}};
    std::mt19937 rng(10249);const char alphabet[]="[](),'+-0123456789.eE \t\n`valuesnullTRUE";
    float values[16];
    for(int iteration=0;iteration<10000;++iteration){
        std::string text;for(unsigned j=0,n=rng()%128;j<n;++j)text+=alphabet[rng()%(sizeof(alphabet)-1)];
        mv_vector_parse(text.data(),text.size(),16,values);
        void *batch=mv_insert_prepare(text.data(),text.size(),cols,3,0);if(batch)mv_insert_free(batch);
    }
    for(const char *text:{"[", "[]", "[-0]", "[1e309]", "[3.4028235677973366e38]", "[1e-999]"})mv_vector_parse(text,strlen(text),1,values);
    mv_column simple[]={{"id",0,0},{"v",2,2}};
    std::string text=" VALUES (1,[-0,-0e0])";auto batch=static_cast<Batch*>(mv_insert_prepare(text.data(),text.size(),simple,2,0));assert(batch);
    uint32_t bits[2];memcpy(bits,batch->data.data()+batch->data.size()-8,8);assert(bits[0]==0&&bits[1]==0x80000000);mv_insert_free(batch);
    auto row=root+"/out.rows",vec=root+"/out.index",key=root+"/out.keys";
    auto run=[&](const std::vector<unsigned char>&data){mv_segment result{};char message[512];return mv_storage_merge(row.c_str(),vec.c_str(),key.c_str(),nullptr,0,data.data(),data.size(),16,0,&result,message,sizeof(message));};
    std::vector<unsigned char> data;put(data,int64_t(1));put(data,uint32_t(8));put(data,int64_t(1));for(int i=0;i<16;i++)put(data,float(i));
    assert(run(data)==0);assert(!std::filesystem::exists(key));std::filesystem::remove(row);std::filesystem::remove(vec);
    auto duplicate=data;duplicate.insert(duplicate.end(),data.begin(),data.end());assert(run(duplicate)!=0);assert(!std::filesystem::exists(row)&&!std::filesystem::exists(vec)&&!std::filesystem::exists(key));
    data.pop_back();assert(run(data)!=0);assert(!std::filesystem::exists(row));
    {File existing(vec.c_str(),"wb");existing.write("keep",4);}
    data.push_back(0);assert(run(data)!=0);assert(!std::filesystem::exists(row));assert(std::filesystem::file_size(vec)==4);std::filesystem::remove(vec);
    double query[16]{};auto ctx=mv_topk_new(query,16,10);assert(ctx);
    std::vector<unsigned char> records;for(int i=0;i<30;i++){put(records,i==0?INT64_MIN:int64_t(i));for(int d=0;d<16;d++)put(records,float(i%3));}
    assert(mv_topk_scan(ctx,records.data(),records.size(),nullptr)==0);int64_t ids[10];double scores[10];assert(mv_topk_result(ctx,ids,scores,10)==10);assert(ids[0]==INT64_MIN&&scores[0]==0.);
    assert(mv_topk_scan(ctx,records.data(),records.size()-1,nullptr)!=0);mv_topk_free(ctx);
    puts("PASS: exact rounding, 10000 vector + 10000 INSERT fuzz inputs, native write/duplicate/truncation/open-failure cleanup, TopK and malformed block");
}
