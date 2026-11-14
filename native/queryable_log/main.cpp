#include "database.hpp"
extern "C" void mv_screen_set_simd(int);
extern "C" void mv_screen_set_float(int);
int main(int argc,char **argv){
    std::ios::sync_with_stdio(false);std::cin.tie(nullptr);using namespace cppdb;
    mv_screen_set_simd(!std::getenv("MINIDB_SIMD")||std::string(std::getenv("MINIDB_SIMD"))!="0");
    mv_screen_set_float(!std::getenv("MINIDB_FLOAT_SCREEN")||std::string(std::getenv("MINIDB_FLOAT_SCREEN"))!="0");
    try{
        LogDatabase db(argc==2?argv[1]:"/data");std::cout<<"{\"type\":\"ready\"}\n"<<std::flush;std::string line;
        while(std::getline(std::cin,line)){
            Json id;std::unique_ptr<LogResponse> result;
            try{
                Json request;try{request=Json::parse(line);}catch(const std::exception &e){fail(e.what(),"invalid_request");}
                if(!request.is_object())fail("request must be object","invalid_request");if(request.contains("id"))id=request.at("id");
                if(!request.contains("op")||!request.at("op").is_string())fail("expected operation","invalid_request");auto op=request.at("op").string();
                if(op=="shutdown"){db.close();std::cout<<"{\"id\":"<<id.dump()<<",\"ok\":true}\n"<<std::flush;return 0;}
                if(op!="execute"||!request.contains("sql")||!request.at("sql").is_string())fail("expected SQL","invalid_request");
                std::string mode="exact";if(request.contains("search_mode")){if(!request.at("search_mode").is_string())fail("invalid mode","invalid_request");mode=request.at("search_mode").string();}result=db.execute(request.at("sql").string(),mode);
            }catch(const SqlError &e){Json out=Json::object(),error=Json::object();out["id"]=id;out["ok"]=false;error["code"]=e.code;error["message"]=e.what();out["error"]=error;std::cout<<out.dump()<<'\n'<<std::flush;continue;}
            Json head=result->status;head["id"]=id;head["ok"]=true;if(!result->query){std::cout<<head.dump()<<'\n'<<std::flush;continue;}
            Json columns=Json::array();for(auto &column:result->columns)columns.push(column);head["columns"]=columns;std::string prefix=head.dump();prefix.pop_back();std::cout<<prefix<<",\"rows\":[";bool first=true;
            auto emit=[&](const std::string &row){if(!first)std::cout<<',';first=false;std::cout<<row;};
            if(result->spill.empty()){for(auto &row:result->rows)emit(row);}else{std::ifstream file(result->spill);std::string row;while(std::getline(file,row))emit(row);if(!file.eof())throw std::runtime_error("read query result");}
            std::cout<<"]}\n"<<std::flush;
        }db.close();return 0;
    }catch(const std::exception &e){std::cerr<<"Queryable-log prototype: "<<e.what()<<'\n';return 1;}
}
