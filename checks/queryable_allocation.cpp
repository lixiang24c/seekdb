// Test-only allocation failures, never linked into the database executable.
#include <cstdlib>
#include <new>
#include <cassert>
static size_t fail_size=0,failures=0;
void *operator new(std::size_t size){if(fail_size&&size==fail_size){fail_size=0;++failures;throw std::bad_alloc();}if(void *p=std::malloc(size?size:1))return p;throw std::bad_alloc();}
void operator delete(void *p)noexcept{std::free(p);}
void operator delete(void *p,std::size_t)noexcept{std::free(p);}
#include "../native/queryable_log/database.hpp"
extern "C" void mv_screen_set_float(int);
int main(int argc,char **argv){
    assert(argc==2);mv_screen_set_float(1);cppdb::LogDatabase db(argv[1]);
    db.execute("CREATE TABLE t(id BIGINT PRIMARY KEY,c BIGINT,v VECTOR(128))");
    std::string vector="[";for(size_t j=0;j<128;j++){if(j)vector+=',';vector+=std::to_string(j%17);}vector+=']';
    for(size_t start=0;start<1152;start+=64){std::string sql="INSERT INTO t VALUES ";for(size_t i=start;i<start+64;i++){if(i>start)sql+=',';sql+='('+std::to_string(i)+','+std::to_string(i%10)+','+vector+')';}db.execute(sql);}
    auto query=[&](int c){auto result=db.execute("SELECT id FROM t WHERE c="+std::to_string(c)+" ORDER BY L2_DISTANCE(v,"+vector+") LIMIT 10");assert(result->spill.empty()&&result->rows.size()==10);for(size_t i=0;i<10;i++)assert(result->rows[i]=='['+std::to_string(c+10*i)+']');};
    fail_size=145;query(0);assert(fail_size==0&&failures==1); // bitmap string
    fail_size=1152;query(1);assert(fail_size==0&&failures==2); // integer column codes
    fail_size=0;query(2);query(3);db.close();
    std::cout<<"{\"status\":\"PASS\",\"injected_cache_allocations\":"<<failures<<"}\n";
}
