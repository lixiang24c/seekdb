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
            auto &column=table.columns[col];if(column.type=="VECTOR")continue;size_t n=8;bool integer=column.type=="BIGINT";
            if(!integer){if(position>size||size-position<4)throw std::runtime_error("incomplete predicate text size");uint32_t length;std::memcpy(&length,data+position,4);position+=4;n=length;}
            if(position>size||n>size-position)throw std::runtime_error("incomplete predicate field");
            while(next<tests.size()&&tests[next].column==col){if(!compare(tests[next],data+position,n,integer))return false;++next;}position+=n;
        }
        if(next!=tests.size())throw std::runtime_error("missing predicate field");return true;
    }
};
