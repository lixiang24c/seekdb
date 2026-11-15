#pragma once
#include <algorithm>
#include <array>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <limits>
#include <map>
#include <memory>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <string_view>
#include <unordered_set>
#include <variant>
#include <vector>
#include "../vendor/yyjson/yyjson.h"
#include "../vendor/utf8proc/utf8proc.h"
namespace cppdb {
struct SqlError:std::runtime_error {
    std::string code;
    SqlError(std::string message,std::string c="unsupported_sql"):std::runtime_error(message),code(std::move(c)){}
};
[[noreturn]] inline void fail(std::string message,std::string code="unsupported_sql"){throw SqlError(std::move(message),std::move(code));}
inline std::string fold(std::string_view source){
    bool ascii=true;for(unsigned char c:source)if(c>=128){ascii=false;break;}
    if(ascii){std::string out(source);for(char &c:out)if(c>='A'&&c<='Z')c+=32;return out;}
    utf8proc_uint8_t *result=nullptr;
    auto size=utf8proc_map(reinterpret_cast<const utf8proc_uint8_t*>(source.data()),source.size(),&result,
        static_cast<utf8proc_option_t>(UTF8PROC_CASEFOLD|UTF8PROC_STABLE));
    if(size<0)fail("invalid Unicode identifier","invalid_value");
    std::unique_ptr<unsigned char,decltype(&std::free)> owner(result,&std::free);
    return std::string(reinterpret_cast<char*>(result),size);
}
inline std::string quote(std::string_view source){
    static const char hex[]="0123456789abcdef";std::string out;out.reserve(source.size()+2);out+='"';
    for(unsigned char c:source){
        if(c=='"'||c=='\\'){out+='\\';out+=char(c);}
        else if(c<32){out+="\\u00";out+=hex[c>>4];out+=hex[c&15];}
        else out+=char(c);
    }
    out+='"';return out;
}
inline std::string_view real_chars(double value,char (&bytes)[64]){
    if(!std::isfinite(value))throw std::runtime_error("nonfinite output");
    if(!value&&std::signbit(value))return "-0.0";
    auto result=std::to_chars(bytes,bytes+sizeof(bytes)-2,value);
    if(result.ec!=std::errc())throw std::runtime_error("float formatting failed");
    if(std::string_view(bytes,size_t(result.ptr-bytes)).find_first_of(".eE")==std::string_view::npos){*result.ptr++='.';*result.ptr++='0';}
    return {bytes,size_t(result.ptr-bytes)};
}
inline std::string real(double value){char bytes[64];return std::string(real_chars(value,bytes));}

struct Json {
    // Protocol IDs are JSON values, not SQL BIGINTs. Retain large integer
    // tokens exactly; metadata integer() still enforces signed 64-bit values.
    struct IntegerToken {std::string text;};
    using Array=std::vector<Json>;using Object=std::map<std::string,Json>;
    std::variant<std::nullptr_t,bool,int64_t,double,std::string,Array,Object,IntegerToken> data=nullptr;
    Json()=default;Json(std::nullptr_t):data(nullptr){}Json(bool v):data(v){}Json(int v):data(int64_t(v)){}
    Json(int64_t v):data(v){}Json(size_t v):data(int64_t(v)){if(v>size_t(INT64_MAX))throw std::runtime_error("metadata integer overflow");}
    Json(double v):data(v){}Json(const char *v):data(std::string(v)){}Json(std::string v):data(std::move(v)){}
    Json(Array v):data(std::move(v)){}Json(Object v):data(std::move(v)){}
    explicit Json(IntegerToken v):data(std::move(v)){}
    static Json array(){return Array{};}static Json object(){return Object{};}
    bool is_null()const{return std::holds_alternative<std::nullptr_t>(data);}
    bool is_string()const{return std::holds_alternative<std::string>(data);}
    bool is_int()const{return std::holds_alternative<int64_t>(data);}
    bool is_array()const{return std::holds_alternative<Array>(data);}
    bool is_object()const{return std::holds_alternative<Object>(data);}
    Array &items(){return std::get<Array>(data);}const Array &items()const{return std::get<Array>(data);}
    Object &fields(){return std::get<Object>(data);}const Object &fields()const{return std::get<Object>(data);}
    const std::string &string()const{return std::get<std::string>(data);}int64_t integer()const{return std::get<int64_t>(data);}
    bool boolean()const{return std::get<bool>(data);}double number()const{return is_int()?double(integer()):std::get<double>(data);}
    Json &operator[](const std::string &key){if(is_null())data=Object{};return fields()[key];}
    const Json &at(const std::string &key)const{return fields().at(key);}
    bool contains(const std::string &key)const{return is_object()&&fields().count(key);}
    void push(Json value){items().push_back(std::move(value));}
    void append_to(std::string &out)const{
        if(is_null())out+="null";
        else if(auto p=std::get_if<bool>(&data))out+=*p?"true":"false";
        else if(auto p=std::get_if<int64_t>(&data))out+=std::to_string(*p);
        else if(auto p=std::get_if<double>(&data))out+=real(*p);
        else if(auto p=std::get_if<IntegerToken>(&data))out+=p->text;
        else if(auto p=std::get_if<std::string>(&data))out+=quote(*p);
        else if(auto p=std::get_if<Array>(&data)){out+='[';bool first=true;for(auto &v:*p){if(!first)out+=',';first=false;v.append_to(out);}out+=']';}
        else {out+='{';bool first=true;for(auto &[k,v]:fields()){if(!first)out+=',';first=false;out+=quote(k);out+=':';v.append_to(out);}out+='}';}
    }
    std::string dump()const{std::string out;append_to(out);return out;}
    static Json from(yyjson_val *value,size_t depth=0){
        if(!value||depth>128)throw std::runtime_error("invalid JSON depth");
        if(yyjson_is_null(value))return nullptr;
        if(yyjson_is_bool(value))return bool(yyjson_get_bool(value));
        if(yyjson_is_sint(value))return int64_t(yyjson_get_sint(value));
        if(yyjson_is_uint(value)){auto n=yyjson_get_uint(value);if(n>INT64_MAX)return Json(IntegerToken{std::to_string(n)});return int64_t(n);}
        if(yyjson_is_raw(value)){std::string token(yyjson_get_raw(value),yyjson_get_len(value));if(token.find_first_of(".eE")!=token.npos)throw std::runtime_error("nonfinite JSON");return Json(IntegerToken{std::move(token)});}
        if(yyjson_is_real(value)){double n=yyjson_get_real(value);if(!std::isfinite(n))throw std::runtime_error("nonfinite JSON");return n;}
        if(yyjson_is_str(value))return std::string(yyjson_get_str(value),yyjson_get_len(value));
        if(yyjson_is_arr(value)){Json out=array();size_t i,max;yyjson_val *item;yyjson_arr_foreach(value,i,max,item)out.push(from(item,depth+1));return out;}
        if(yyjson_is_obj(value)){Json out=object();size_t i,max;yyjson_val *key,*item;yyjson_obj_foreach(value,i,max,key,item){
            std::string name(yyjson_get_str(key),yyjson_get_len(key));if(out.contains(name))throw std::runtime_error("duplicate JSON key");out[name]=from(item,depth+1);
        }return out;}
        throw std::runtime_error("invalid JSON value");
    }
    static Json parse(std::string_view text){
        yyjson_read_err error;auto *doc=yyjson_read_opts(const_cast<char*>(text.data()),text.size(),YYJSON_READ_BIGNUM_AS_RAW,nullptr,&error);
        if(!doc)throw std::runtime_error(std::string("invalid JSON: ")+error.msg);
        std::unique_ptr<yyjson_doc,decltype(&yyjson_doc_free)> owner(doc,&yyjson_doc_free);
        return from(yyjson_doc_get_root(doc));
    }
};
inline bool unicode_space(int32_t c){return (c>=9&&c<=13)||(c>=28&&c<=32)||c==0x85||c==0xa0||c==0x1680||(c>=0x2000&&c<=0x200a)||c==0x2028||c==0x2029||c==0x202f||c==0x205f||c==0x3000;}
inline size_t unicode_length(std::string_view text){
    size_t count=0,offset=0;while(offset<text.size()){
        int32_t cp;auto size=utf8proc_iterate(reinterpret_cast<const uint8_t*>(text.data()+offset),text.size()-offset,&cp);
        if(size<0)fail("invalid Unicode string","invalid_value");offset+=size;++count;
    }return count;
}
}
