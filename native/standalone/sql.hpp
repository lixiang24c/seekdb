#pragma once
#include "common.hpp"
namespace cppdb {
inline uint64_t compact_vector_accepts=0;
struct Token {enum Kind{End,Identifier,String,Number,Symbol}kind=End;std::string text;};
class Lexer {
    std::string_view input;size_t pos=0;bool comments;
    std::pair<int,size_t> decimal(size_t at)const{
        if(at>=input.size())return {-1,0};unsigned char c=input[at];
        if(c>='0'&&c<='9')return {c-'0',1};if(c<128)return {-1,0};
        int32_t cp;auto n=utf8proc_iterate(reinterpret_cast<const uint8_t*>(input.data()+at),input.size()-at,&cp);
        if(n<0||utf8proc_category(cp)!=UTF8PROC_CATEGORY_ND)return {-1,0};
        int32_t base=cp;while(base&&utf8proc_category(base-1)==UTF8PROC_CATEGORY_ND)--base;
        return {int((cp-base)%10),size_t(n)};
    }
    void spaces(){
        while(pos<input.size()){
            unsigned char c=input[pos];int32_t cp=c;size_t n=1;
            if(c>=128){auto decoded=utf8proc_iterate(reinterpret_cast<const uint8_t*>(input.data()+pos),input.size()-pos,&cp);if(decoded<0)fail("invalid SQL Unicode");n=decoded;}
            if(unicode_space(cp)){pos+=n;continue;}
            if(comments&&input.substr(pos,2)=="--"){auto end=input.find('\n',pos+2);pos=end==input.npos?input.size():end+1;continue;}
            if(comments&&input.substr(pos,2)=="/*"){auto end=input.find("*/",pos+2);if(end==input.npos)fail("unterminated comment");pos=end+2;continue;}
            break;
        }
    }
public:
    explicit Lexer(std::string_view text,bool allow_comments=true):input(text),comments(allow_comments){}
    size_t position()const{return pos;}
    bool compact_vector(std::string &out){
        static const bool enabled=!std::getenv("MINIDB_COMPACT_VECTOR")||std::string(std::getenv("MINIDB_COMPACT_VECTOR"))!="0";
        if(!enabled)return false;
        size_t at=pos,start=pos-1;
        auto space=[&](){while(at<input.size()&&std::string_view(" \t\n\r\f\v").find(input[at])!=std::string_view::npos)++at;};
        auto digits=[&](){size_t begin=at;while(at<input.size()&&input[at]>='0'&&input[at]<='9')++at;return at-begin;};
        space();if(at<input.size()&&input[at]==']'){out=std::string(input.substr(start,++at-start));pos=at;++compact_vector_accepts;return true;}
        while(at<input.size()){
            if(input[at]=='+'||input[at]=='-')++at;
            size_t n=digits();if(at<input.size()&&input[at]=='.'){++at;n+=digits();}if(!n)return false;
            if(at<input.size()&&(input[at]=='e'||input[at]=='E')){++at;if(at<input.size()&&(input[at]=='+'||input[at]=='-'))++at;if(!digits())return false;}
            space();if(at==input.size())return false;
            if(input[at]==']'){out=std::string(input.substr(start,++at-start));pos=at;++compact_vector_accepts;return true;}
            if(input[at++]!=',')return false;space();
        }return false;
    }
    Token next(){
        spaces();if(pos==input.size())return {};
        char c=input[pos];
        if(c=='\''||c=='`'){
            char quote=c;++pos;std::string text;
            while(true){auto end=input.find(quote,pos);if(end==input.npos)fail("unterminated quoted value");text.append(input.substr(pos,end-pos));pos=end+1;
                if(pos<input.size()&&input[pos]==quote){text+=quote;++pos;continue;}break;}
            return {quote=='\''?Token::String:Token::Identifier,std::move(text)};
        }
        bool sign=c=='+'||c=='-';size_t start=pos+(sign?1:0);
        if(decimal(start).first>=0||(start<input.size()&&input[start]=='.'&&decimal(start+1).first>=0)){
            std::string text;if(sign)text+=input[pos++];
            auto digits=[&](){size_t n=0;while(true){auto [v,bytes]=decimal(pos);if(v<0)break;text+=char('0'+v);pos+=bytes;++n;}return n;};
            digits();if(pos<input.size()&&input[pos]=='.'){text+=input[pos++];digits();}
            if(pos<input.size()&&(input[pos]=='e'||input[pos]=='E')){text+=input[pos++];if(pos<input.size()&&(input[pos]=='+'||input[pos]=='-'))text+=input[pos++];if(!digits())fail("invalid exponent");}
            return {Token::Number,std::move(text)};
        }
        auto letter=[](char x){return (x>='a'&&x<='z')||(x>='A'&&x<='Z')||x=='_';};
        if(letter(c)){size_t start=pos++;while(pos<input.size()&&(letter(input[pos])||(input[pos]>='0'&&input[pos]<='9')))++pos;return {Token::Identifier,std::string(input.substr(start,pos-start))};}
        if(input.substr(pos,2)=="<="||input.substr(pos,2)==">="){std::string text(input.substr(pos,2));pos+=2;return {Token::Symbol,text};}
        if(std::string_view("(),[]=<>*;").find(c)!=std::string_view::npos){++pos;return {Token::Symbol,std::string(1,c)};}
        fail("unexpected SQL character");
    }
};
struct Value {
    enum Kind{Numeric,Text,Vector}kind=Text;std::string text;std::vector<Value> elements;bool integral=false;
    static Value number(std::string text){Value v;v.kind=Numeric;v.integral=text.find_first_of(".eE")==text.npos;v.text=std::move(text);return v;}
    double real_value(std::string code="invalid_value")const{
        if(kind!=Numeric)fail("expected numeric value",code);const char *start=text.data();if(*start=='+')++start;
        double out;auto r=std::from_chars(start,text.data()+text.size(),out,std::chars_format::general);
        if(r.ec==std::errc::result_out_of_range){char *end=nullptr;out=std::strtod(text.c_str(),&end);if(end!=text.c_str()+text.size()||!std::isfinite(out))fail("invalid numeric value",code);return out;}
        if(r.ec!=std::errc()||r.ptr!=text.data()+text.size()||!std::isfinite(out))fail("invalid numeric value",code);return out;
    }
    int64_t bigint()const{
        if(kind!=Numeric||!integral)fail("expected signed BIGINT","invalid_value");const char *start=text.data();if(*start=='+')++start;
        int64_t out;auto r=std::from_chars(start,text.data()+text.size(),out);if(r.ec!=std::errc()||r.ptr!=text.data()+text.size())fail("expected signed BIGINT","invalid_value");return out;
    }
    Json json()const{
        if(kind==Text)return text;
        if(kind==Vector){Json out=Json::array();if(!text.empty()){Lexer lex(text);lex.next();for(auto t=lex.next();t.text!="]";t=lex.next())if(t.text!=",")out.push(Value::number(t.text).json());}else for(auto &v:elements)out.push(v.json());return out;}
        if(integral){const char *start=text.data();if(*start=='+')++start;int64_t out;auto r=std::from_chars(start,text.data()+text.size(),out);if(r.ec==std::errc()&&r.ptr==text.data()+text.size())return out;Json tagged=Json::object();tagged["__integer"]=text;return tagged;}
        return real_value("unsupported_sql");
    }
};
struct Expr {
    bool distance=false;std::string name;Value query;
    Json json()const{Json out=Json::array();out.push(distance?"distance":"column");out.push(name);if(distance)out.push(query.json());return out;}
};
struct Condition {std::string column,op;Value value;};
struct Column {std::string name,type;size_t n=0;bool primary=false;};
struct Projection {Expr expr;std::optional<std::string> alias;};
struct Ordering {Expr expr;bool descending=false;};
struct Statement {
    std::string op,table,index,index_column;std::vector<Column> columns;
    bool star=false,insert_columns=false;std::vector<std::string> insert_names;
    std::vector<std::vector<Value>> rows;std::vector<Projection> projection;std::vector<Ordering> order;
    std::vector<Condition> conditions;std::vector<std::pair<std::string,Value>> sets;std::optional<size_t> limit;
    Json json()const{
        Json out=Json::object();out["op"]=op;if(op!="begin"&&op!="commit"&&op!="rollback")out["table"]=table;
        auto where=[&](){Json a=Json::array();for(auto &c:conditions){Json x=Json::array();x.push(c.column);x.push(c.op);x.push(c.value.json());a.push(x);}return a;};
        if(op=="create"){Json cols=Json::array();for(auto &c:columns){Json x=Json::object();x["name"]=c.name;x["type"]=c.type;x["n"]=c.n;x["pk"]=c.primary;cols.push(x);}out["cols"]=cols;}
        if(op=="index"){out["name"]=index;out["col"]=index_column;}
        if(op=="insert"){
            Json names=Json::array();for(auto &n:insert_names)names.push(n);out["cols"]=insert_columns?names:Json(nullptr);
            Json data=Json::array();for(auto &row:rows){Json a=Json::array();for(auto &v:row)a.push(v.json());data.push(a);}out["rows"]=data;
        }
        if(op=="select"){
            Json project=Json::array();for(auto &p:projection){Json a=Json::array();a.push(p.expr.json());a.push(p.alias?Json(*p.alias):Json(nullptr));project.push(a);}out["projection"]=star?Json(nullptr):project;
            Json sorted=Json::array();for(auto &o:order){Json a=Json::array();a.push(o.expr.json());a.push(o.descending);sorted.push(a);}out["order"]=sorted;out["where"]=where();out["limit"]=limit?Json(*limit):Json(nullptr);
        }
        if(op=="update"){Json a=Json::array();for(auto &[name,value]:sets){Json x=Json::array();x.push(name);x.push(value.json());a.push(x);}out["sets"]=a;out["where"]=where();}
        if(op=="delete")out["where"]=where();return out;
    }
};
class Parser {
    Lexer lexer;std::optional<Token> look;
    const Token &peek(){if(!look)look=lexer.next();return *look;}
    Token pop(){peek();Token out=std::move(*look);look.reset();return out;}
public:
    explicit Parser(std::string_view text):lexer(text){}
    size_t position()const{return lexer.position();}
    bool take(std::string_view text){
        auto &t=peek();if(t.kind==Token::String||t.kind==Token::End)return false;
        bool ascii=true;for(unsigned char c:t.text)if(c>=128){ascii=false;break;}
        if(!ascii){if(fold(t.text)!=fold(text))return false;}
        else {if(t.text.size()!=text.size())return false;for(size_t i=0;i<text.size();i++){unsigned char a=t.text[i],b=text[i];if(a>='A'&&a<='Z')a+=32;if(b>='A'&&b<='Z')b+=32;if(a!=b)return false;}}
        look.reset();return true;
    }
    void need(std::string_view text){if(!take(text))fail("expected "+std::string(text));}
    std::string ident(){if(peek().kind!=Token::Identifier)fail("expected identifier");return pop().text;}
    Value value(){
        if(take("[")){Value out;out.kind=Value::Vector;if(lexer.compact_vector(out.text))return out;if(!take("]")){do{Value v=value();if(v.kind!=Value::Numeric)fail("invalid vector element","invalid_vector");out.elements.push_back(std::move(v));if(take("]"))return out;need(",");}while(true);}return out;}
        Token t=pop();if(t.kind==Token::String){Value out;out.text=std::move(t.text);return out;}if(t.kind==Token::Number)return Value::number(std::move(t.text));fail("unsupported value");
    }
    size_t integer(bool positive=false){
        Value v=value();if(v.kind!=Value::Numeric||!v.integral||v.text[0]=='-'){
            if(v.kind==Value::Numeric&&v.integral&&!positive&&v.text[0]=='-'&&v.text.find_first_not_of('0',1)==v.text.npos)return 0;
            fail("expected nonnegative integer");
        }
        const char *start=v.text.data();if(*start=='+')++start;size_t out;auto r=std::from_chars(start,v.text.data()+v.text.size(),out);
        if(r.ec!=std::errc()||r.ptr!=v.text.data()+v.text.size()||(positive&&!out))fail("expected nonnegative integer");return out;
    }
    Expr expr(){Expr out;out.name=ident();if(fold(out.name)=="l2_distance"&&take("(")){out.distance=true;out.name=ident();need(",");out.query=value();need(")");}return out;}
    std::vector<Condition> conditions(){
        std::vector<Condition> out;if(take("WHERE"))do{
            Condition c;c.column=ident();bool found=false;for(auto op:{"=","<=",">=","<",">"})if(take(op)){c.op=op;found=true;break;}if(!found)fail("unsupported comparison");c.value=value();out.push_back(std::move(c));
        }while(take("AND"));return out;
    }
    Statement parse(){
        Statement st;
        if(take("CREATE")){
            if(take("TABLE")){
                st.op="create";st.table=ident();need("(");std::vector<std::string> primary;
                while(true){
                    if(take("PRIMARY")){need("KEY");need("(");primary.push_back(ident());need(")");}
                    else{Column c;c.name=ident();auto type=fold(ident());if(type=="bigint")c.type="BIGINT";else if(type=="varchar")c.type="VARCHAR";else if(type=="vector")c.type="VECTOR";else fail("unsupported column type");
                        if(c.type!="BIGINT"){need("(");c.n=integer(true);need(")");}if(take("PRIMARY")){need("KEY");c.primary=true;}st.columns.push_back(std::move(c));}
                    if(take(")"))break;need(",");
                }
                for(auto &name:primary){Column *found=nullptr;for(auto &c:st.columns)if(fold(c.name)==fold(name)){if(found)fail("invalid primary key");found=&c;}if(!found||found->primary)fail("invalid primary key");found->primary=true;}
            }else{need("VECTOR");need("INDEX");st.op="index";st.index=ident();need("ON");st.table=ident();need("(");st.index_column=ident();need(")");}
        }else if(take("INSERT")){
            need("INTO");st.op="insert";st.table=ident();if(take("(")){st.insert_columns=true;do{st.insert_names.push_back(ident());}while(take(","));need(")");}need("VALUES");
            do{need("(");std::vector<Value> row;do{row.push_back(value());}while(take(","));need(")");st.rows.push_back(std::move(row));}while(take(","));
        }else if(take("SELECT")){
            st.op="select";st.star=take("*");if(!st.star)do{Projection p;p.expr=expr();if(take("AS"))p.alias=ident();st.projection.push_back(std::move(p));}while(take(","));need("FROM");st.table=ident();st.conditions=conditions();
            if(take("ORDER")){need("BY");do{Ordering o;o.expr=expr();o.descending=take("DESC");if(!o.descending)take("ASC");st.order.push_back(std::move(o));}while(take(","));}if(take("LIMIT"))st.limit=integer();
        }else if(take("UPDATE")){
            st.op="update";st.table=ident();need("SET");do{auto name=ident();need("=");st.sets.emplace_back(std::move(name),value());}while(take(","));st.conditions=conditions();
        }else if(take("DELETE")){need("FROM");st.op="delete";st.table=ident();st.conditions=conditions();}
        else if(take("BEGIN")){st.op="begin";take("TRANSACTION");}else if(take("COMMIT"))st.op="commit";else if(take("ROLLBACK"))st.op="rollback";else fail("unsupported SQL statement");
        take(";");if(peek().kind!=Token::End)fail("unexpected trailing SQL");return st;
    }
};
}
