#include "database.hpp"
extern "C" void mv_screen_set_simd(int);
extern "C" void mv_screen_set_float(int);
// Keep the parsed document alive until SQL execution completes. Only a small,
// fully recognized execute envelope is admitted; all other packets retain the
// generic decoder, including duplicate/unknown fields and complex protocol IDs.
struct ExecuteView {
    std::unique_ptr<yyjson_doc,decltype(&yyjson_doc_free)> document{nullptr,&yyjson_doc_free};
    yyjson_val *id=nullptr;std::string_view sql,mode="exact";bool valid=false;
    explicit ExecuteView(std::string_view text){
        yyjson_read_err error;document.reset(yyjson_read_opts(const_cast<char*>(text.data()),text.size(),YYJSON_READ_BIGNUM_AS_RAW,nullptr,&error));
        if(!document)return;auto *root=yyjson_doc_get_root(document.get());if(!yyjson_is_obj(root)||yyjson_obj_size(root)>4)return;
        unsigned seen=0;yyjson_val *key,*value;size_t i,n;bool execute=false;
        yyjson_obj_foreach(root,i,n,key,value){
            std::string_view name(yyjson_get_str(key),yyjson_get_len(key));unsigned bit=name=="id"?1:name=="op"?2:name=="sql"?4:name=="search_mode"?8:0;
            if(!bit||(seen&bit))return;seen|=bit;
            if(bit==1){if(yyjson_is_arr(value)||yyjson_is_obj(value))return;id=value;}
            else{if(!yyjson_is_str(value))return;std::string_view field(yyjson_get_str(value),yyjson_get_len(value));if(bit==2)execute=field=="execute";else if(bit==4)sql=field;else mode=field;}
        }
        valid=execute&&(seen&6)==6;
    }
};
int main(int argc,char **argv){
    std::ios::sync_with_stdio(false);std::cin.tie(nullptr);using namespace cppdb;
    mv_screen_set_simd(!std::getenv("MINIDB_SIMD")||std::string(std::getenv("MINIDB_SIMD"))!="0");
    mv_screen_set_float(!std::getenv("MINIDB_FLOAT_SCREEN")||std::string(std::getenv("MINIDB_FLOAT_SCREEN"))!="0");
    try{
        bool request_view=!std::getenv("MINIDB_REQUEST_VIEW")||std::string(std::getenv("MINIDB_REQUEST_VIEW"))!="0";LogDatabase db(argc==2?argv[1]:"/data");std::cout<<"{\"type\":\"ready\"}\n"<<std::flush;std::string line;
        while(std::getline(std::cin,line)){
            Json id;std::unique_ptr<LogResponse> result;
            try{
                if(request_view){ExecuteView view(line);if(view.valid){try{if(view.id)id=Json::from(view.id);}catch(const std::exception &e){fail(e.what(),"invalid_request");}result=db.execute(view.sql,std::string(view.mode));}}
                if(!result){
                Json request;try{request=Json::parse(line);}catch(const std::exception &e){fail(e.what(),"invalid_request");}
                if(!request.is_object())fail("request must be object","invalid_request");if(request.contains("id"))id=request.at("id");
                if(!request.contains("op")||!request.at("op").is_string())fail("expected operation","invalid_request");auto op=request.at("op").string();
                if(op=="shutdown"){db.close();std::cout<<"{\"id\":"<<id.dump()<<",\"ok\":true}\n"<<std::flush;return 0;}
                if(op!="execute"||!request.contains("sql")||!request.at("sql").is_string())fail("expected SQL","invalid_request");
                std::string mode="exact";if(request.contains("search_mode")){if(!request.at("search_mode").is_string())fail("invalid mode","invalid_request");mode=request.at("search_mode").string();}result=db.execute(request.at("sql").string(),mode);
                }
            }catch(const SqlError &e){Json out=Json::object(),error=Json::object();out["id"]=id;out["ok"]=false;error["code"]=e.code;error["message"]=e.what();out["error"]=error;std::cout<<out.dump()<<'\n'<<std::flush;continue;}
            if(result->compiled){result->emit_compiled(std::cout,id);std::cout<<std::flush;continue;}
            Json head=result->status;head["id"]=id;head["ok"]=true;if(!result->query){std::cout<<head.dump()<<'\n'<<std::flush;continue;}
            Json columns=Json::array();for(auto &column:result->columns)columns.push(column);head["columns"]=columns;std::string prefix=head.dump();prefix.pop_back();std::cout<<prefix<<",\"rows\":[";bool first=true;
            auto emit=[&](const std::string &row){if(!first)std::cout<<',';first=false;std::cout<<row;};
            if(result->spill.empty()){for(auto &row:result->rows)emit(row);}else{std::ifstream file(result->spill);std::string row;while(std::getline(file,row))emit(row);if(!file.eof())throw std::runtime_error("read query result");}
            std::cout<<"]}\n"<<std::flush;
        }db.close();return 0;
    }catch(const std::exception &e){std::cerr<<"Queryable-log prototype: "<<e.what()<<'\n';return 1;}
}
