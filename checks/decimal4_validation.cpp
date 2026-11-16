// Independent from_chars oracle plus original parser fallback; no engine changes.
#include "../native/storage.cpp"
#include <iostream>
#include <random>
#include <array>
int main(){
 size_t numbers=0,vectors=0,mutations=0;std::mt19937 rng(42);
 auto literal=[&](const std::string &text,bool required){double got=0,want=0;const char *after=nullptr;bool ok=decimal4(text.data(),text.data()+text.size(),after,got);if(required&&!ok)throw std::runtime_error("decimal4 rejected coefficient");if(ok){auto first=text.data()+(*text.data()=='+');auto r=std::from_chars(first,after,want);if(r.ec!=std::errc()||r.ptr!=after||memcmp(&got,&want,8))throw std::runtime_error("decimal4 binary64 mismatch");}++numbers;};
 auto vector=[&](const std::string &text,size_t dim){std::vector<float> a(dim),b(dim);for(bool bare:{false,true}){int x=vector_parse_impl(text.data(),text.size(),dim,a.data(),bare),y=vector_parse_reference(text.data(),text.size(),dim,b.data(),bare);if(x!=y||(!x&&memcmp(a.data(),b.data(),dim*4)))throw std::runtime_error("vector reference mismatch: "+text.substr(0,100));++vectors;}};
 for(unsigned i=0;i<100000;i++){char text[32];std::snprintf(text,sizeof(text),"%u.%04u,",i/10000,i%10000);for(auto sign:{"","+","-"})literal(std::string(sign)+text,true);}
 std::string sample="-1.2345,";
 for(size_t j=0;j<sample.size();j++)for(unsigned c=0;c<256;c++){auto x=sample;x[j]=char(c);literal(x,false);++mutations;}
 for(size_t dim:{size_t(1),size_t(16),size_t(128),size_t(512),size_t(528),size_t(4096)}){
  std::string normal="[";for(size_t j=0;j<dim;j++){if(j)normal+=',';normal+=j%3==0?"-0.0000":j%3==1?"+9.9999":"0.1234";}normal+=']';vector(normal,dim);
  for(auto value:{"1e-30","3.4028234663852886e38","1e309","1e39","-1e-46","-0","+0","10.0000","nan","1.23456","1.2345x",".0001","1.","--1"}){auto x=normal;x.replace(1,x.find(',',1)==x.npos?x.size()-2:x.find(',',1)-1,value);vector(x,dim);}
  for(auto pad:{" ","\t\n","\r\f\v"})vector(std::string(pad)+normal+pad,dim);
  vector(normal,dim+1);
 }
 std::string base="[1.2345,-0.0000,9.9999]";
 for(size_t j=0;j<base.size();j++)for(unsigned c=0;c<256;c++){auto x=base;x[j]=char(c);vector(x,3);++mutations;}
 std::cout<<"{\"status\":\"PASS\",\"numbers\":"<<numbers<<",\"vector_checks\":"<<vectors<<",\"mutations\":"<<mutations<<"}\n";
}
