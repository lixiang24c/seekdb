#pragma once
// Bound once per query. Values and operator decoding never allocate per row.
class LogPredicate {
    struct Test {size_t column,offset;bool fixed;int op;Cell value;};
    const Table &table;std::vector<Test> tests;bool fixed=true;size_t prefix=0;
    static bool accept(int cmp,int op){return op==0?cmp==0:op==1?cmp<0:op==2?cmp<=0:op==3?cmp>0:cmp>=0;}
    static bool compare(const Test &test,const unsigned char *data,size_t n,bool integer){
        int cmp;
        if(integer){if(n!=8)throw std::runtime_error("invalid predicate integer");int64_t value;std::memcpy(&value,data,8);auto target=std::get<int64_t>(test.value);cmp=value<target?-1:value>target?1:0;}
        else{auto &target=std::get<std::string>(test.value);cmp=std::string_view(reinterpret_cast<const char*>(data),n).compare(target);}
        return accept(cmp,test.op);
    }
    static bool compare_text(const Test &test,std::string_view value){auto &target=std::get<std::string>(test.value);return accept(value.compare(target),test.op);}
public:
    LogPredicate(const Table &schema,const std::vector<BoundCondition> &conditions):table(schema){
        for(auto &c:conditions){size_t offset=0;bool known=true;
            for(size_t i=0;i<c.column;i++){auto &col=table.columns[i];if(col.type=="BIGINT")offset+=8;else if(col.type=="VARCHAR"){known=false;break;}}
            int op=c.op=="="?0:c.op=="<"?1:c.op=="<="?2:c.op==">"?3:c.op==">="?4:-1;
            if(op<0)throw std::runtime_error("invalid bound predicate");
            known=known&&table.columns[c.column].type=="BIGINT";fixed=fixed&&known;if(known)prefix=std::max(prefix,offset+8);tests.push_back({c.column,offset,known,op,c.value});
        }
        std::stable_sort(tests.begin(),tests.end(),[](auto &a,auto &b){return a.column<b.column;});
    }
    size_t read_size(size_t size)const{return fixed?std::min(size,prefix):size;}
    bool matches(const unsigned char *data,size_t size)const{
        if(fixed){for(auto &test:tests){if(test.offset>size||size-test.offset<8)throw std::runtime_error("incomplete predicate field");if(!compare(test,data+test.offset,8,true))return false;}return true;}
        size_t position=0,next=0;
        for(size_t col=0;col<table.columns.size()&&next<tests.size();col++){
            auto &column=table.columns[col];if(column.type=="VECTOR")continue;
            if(column.type=="BIGINT"){
                if(position>size||size-position<8)throw std::runtime_error("incomplete predicate integer");
                while(next<tests.size()&&tests[next].column==col){if(!compare(tests[next],data+position,8,true))return false;++next;}position+=8;continue;
            }
            if(position>size||size-position<4)throw std::runtime_error("incomplete predicate text size");uint32_t tag;std::memcpy(&tag,data+position,4);position+=4;
            if(text_compressed(tag)){
                size_t bytes=tag&0x3fffffffu;if(position>size||size-position<4+bytes)throw std::runtime_error("incomplete predicate compressed text");
                if(next<tests.size()&&tests[next].column==col){
                    uint32_t original;std::memcpy(&original,data+position,4);std::string value(original,'\0');uLongf length=original;
                    if(uncompress(reinterpret_cast<Bytef*>(value.data()),&length,data+position+4,bytes)!=Z_OK||length!=original)throw std::runtime_error("invalid predicate compressed text");
                    while(next<tests.size()&&tests[next].column==col){if(!compare_text(tests[next],value))return false;++next;}
                }
                position+=4+bytes;
            }else{
                if(tag>size-position)throw std::runtime_error("incomplete predicate text");
                while(next<tests.size()&&tests[next].column==col){if(!compare_text(tests[next],std::string_view(reinterpret_cast<const char*>(data+position),tag)))return false;++next;}
                position+=tag;
            }
        }
        if(next!=tests.size())throw std::runtime_error("missing predicate field");return true;
    }
};
