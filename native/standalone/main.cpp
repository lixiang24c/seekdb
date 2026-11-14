#include "engine.hpp"
#include <cassert>
extern "C" void mv_screen_set_simd(int);
extern "C" void mv_screen_set_float(int);
int main(int argc,char **argv){
    std::ios::sync_with_stdio(false);std::cin.tie(nullptr);
    using namespace cppdb;
    mv_screen_set_simd(!std::getenv("MINIDB_SIMD")||std::string(std::getenv("MINIDB_SIMD"))!="0");
    mv_screen_set_float(!std::getenv("MINIDB_FLOAT_SCREEN")||std::string(std::getenv("MINIDB_FLOAT_SCREEN"))!="0");
    if(argc==3&&std::string(argv[1])=="--storage-test"){
        std::filesystem::path root=argv[2];std::filesystem::create_directories(root);mv_storage_set_durable(0);
        Table table;table.columns={{"id","BIGINT",0,true},{"text","VARCHAR",10000,false},{"v","VECTOR",32,false}};table.pk=0;table.vector_column=2;table.count=2;
        std::vector<Row> expected;for(int64_t id:{-5,2})expected.push_back({id,std::string("hé界"),std::vector<float>(32,float(id))});
        {
            Journal log(root);Part part;
            {SegmentWriter writer(root,table,"mv-test.rows","mv-test.index","mv-test.keys");for(auto &row:expected){Bytes vectors(128);auto &v=std::get<std::vector<float>>(row[2]);std::memcpy(vectors.data(),v.data(),128);writer.append(std::get<int64_t>(row[0]),Codec{table}.scalars(row),vectors);}part=writer.finish();}
            table.parts.push_back(part);log.commit({{"t",table}},{});
        }
        for(int pass=0;pass<3;pass++){
            if(pass==1&&::truncate((root/"mv-test.index").c_str(),9))throw std::runtime_error("test truncate failed");
            Journal log(root);assert(log.tables.at("t").count==2);Scanner scan(root,log.tables.at("t"));int64_t id;Bytes scalar,vector;size_t seen=0;
            while(scan.next(id,scalar,vector)){assert(Codec{log.tables.at("t")}.decode(scalar,vector)==expected.at(seen++));}assert(seen==2);
        }
        std::cout<<"{\"status\":\"PASS\",\"physical_rows\":2,\"restarts\":3,\"partial_materialization_rebuilt\":true}\n";return 0;
    }
    if(argc!=2||std::string(argv[1])!="--parse-only"){
        try {
            Database db(argc==2?argv[1]:"/data");
            std::cout<<"{\"type\":\"ready\"}\n"<<std::flush;
            std::string line;
            while(std::getline(std::cin,line)){
                Json ident;std::unique_ptr<Response> result;
                try {
                    Json request;try{request=Json::parse(line);}catch(const std::exception &e){fail(e.what(),"invalid_request");}
                    if(!request.is_object())fail("request must be object","invalid_request");
                    if(request.contains("id"))ident=request.at("id");
                    if(!request.contains("op")||!request.at("op").is_string())fail("expected execute or shutdown","invalid_request");
                    auto op=request.at("op").string();
                    if(op=="shutdown"){db.close();std::cout<<"{\"id\":"<<ident.dump()<<",\"ok\":true}\n"<<std::flush;return 0;}
                    if(op!="execute"||!request.contains("sql")||!request.at("sql").is_string())fail("expected execute SQL","invalid_request");
                    std::string mode="exact";
                    if(request.contains("search_mode")){if(!request.at("search_mode").is_string())fail("invalid search mode","invalid_request");mode=request.at("search_mode").string();}
                    result=db.execute(request.at("sql").string(),mode);
                }catch(const SqlError &error){
                    Json response=Json::object(),e=Json::object();response["id"]=ident;response["ok"]=false;e["code"]=error.code;e["message"]=error.what();response["error"]=e;std::cout<<response.dump()<<'\n'<<std::flush;continue;
                }
                Json head=result->status;head["id"]=ident;head["ok"]=true;
                if(!result->query){std::cout<<head.dump()<<'\n'<<std::flush;continue;}
                Json columns=Json::array();for(auto &name:result->columns)columns.push(name);head["columns"]=columns;
                std::string prefix=head.dump();prefix.pop_back();std::cout<<prefix<<",\"rows\":[";bool first=true;
                auto emit=[&](const std::string &row){if(!first)std::cout<<',';first=false;std::cout<<row;};
                if(result->spill.empty()){for(auto &row:result->rows)emit(row);}else{std::ifstream file(result->spill);std::string row;while(std::getline(file,row))emit(row);if(!file.eof())throw std::runtime_error("read result spill");}
                std::cout<<"]}\n"<<std::flush;
            }
            db.close();return 0;
        }catch(const std::exception &e){std::cerr<<"MiniVectorDB native: "<<e.what()<<'\n';return 1;}
    }
    std::string line;
    while(std::getline(std::cin,line)){
        Json response=Json::object();
        try{
            Json request=Json::parse(line);
            if(!request.is_object()||!request.contains("sql")||!request.at("sql").is_string())fail("expected SQL","invalid_request");
            Statement st=Parser(request.at("sql").string()).parse();response["ok"]=true;response["statement"]=st.json();
        }catch(const SqlError &error){response["ok"]=false;Json e=Json::object();e["code"]=error.code;e["message"]=error.what();response["error"]=e;}
        catch(const std::exception &error){response["ok"]=false;Json e=Json::object();e["code"]="invalid_request";e["message"]=error.what();response["error"]=e;}
        std::cout<<response.dump()<<'\n'<<std::flush;
    }
    return 0;
}
