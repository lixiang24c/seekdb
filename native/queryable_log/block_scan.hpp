#pragma once
// Included inside cppdb after LogExtent. Each stream has a bounded read window;
// a single oversized field may exceed the window, never the entire table.
class LogReadWindow {
    int fd;uint64_t end,start=0;Bytes bytes;
public:
    LogReadWindow(int descriptor,uint64_t limit):fd(descriptor),end(limit){}
    const unsigned char *get(uint64_t offset,size_t n){
        if(offset>end||n>end-offset)throw std::runtime_error("scan outside extent");
        if(offset>=start&&offset-start<=bytes.size()&&n<=bytes.size()-(offset-start))return bytes.data()+(offset-start);
        start=offset;bytes.resize(std::max(n,size_t(std::min(uint64_t(65536),end-offset))));
        read_at(fd,bytes.data(),bytes.size(),offset);return bytes.data();
    }
};
class LogBlockScanner {
    const Table &table;const LogExtent &extent;const std::vector<bool> &wanted;
    size_t ordinal,last;LogReadWindow keys,scalars,vectors;std::optional<LogPredicate> predicate;
public:
    LogBlockScanner(int fd,const Table &schema,const LogExtent &part,const std::vector<bool> &needed,size_t first,size_t stop,const std::vector<BoundCondition> *conditions=nullptr):
        table(schema),extent(part),wanted(needed),ordinal(first),last(stop),
        keys(fd,part.rows+part.row_bytes),scalars(fd,part.rows+part.keys),vectors(fd,part.vectors+part.vector_bytes){if(conditions&&!conditions->empty())predicate.emplace(schema,*conditions);}
    bool next(Row &row,size_t &position){
        while(ordinal<last){position=ordinal++;if(!extent.visible(position))continue;
            uint64_t offset;std::memcpy(&offset,keys.get(extent.rows+extent.keys+16+position*16,8),8);
            uint32_t n;std::memcpy(&n,scalars.get(extent.rows+offset,4),4);
            if(predicate){auto *prefix=scalars.get(extent.rows+offset+4,predicate->read_size(n));if(!predicate->matches(prefix,n))continue;}
            if(offset>extent.keys||extent.keys-offset<4||n>extent.keys-offset-4)throw std::runtime_error("row outside scalar extent");
            row=Row(table.columns.size());size_t pos=0;
            for(size_t col=0;col<table.columns.size();col++){
                auto &column=table.columns[col];bool keep=wanted[col];
                if(column.type=="VECTOR"){
                    if(keep){size_t width=table.dimension()*4;std::vector<float> value(table.dimension());if(extent.codec){size_t packed=extent.width(table.dimension());auto *data=vectors.get(extent.vectors+16+position*extent.stride(table.dimension()),packed);if(!mv_codec_decode(data,table.dimension(),reinterpret_cast<unsigned char*>(value.data())))throw std::runtime_error("invalid scanned packed vector");}else{auto *data=vectors.get(extent.vectors+16+position*(8+width),width);std::memcpy(value.data(),data,width);}row[col]=std::move(value);}continue;
                }
                if(column.type=="BIGINT"){
                    if(pos>n||n-pos<8)throw std::runtime_error("incomplete projected integer");
                    if(keep){int64_t value;std::memcpy(&value,scalars.get(extent.rows+offset+4+pos,8),8);row[col]=value;}pos+=8;
                }else{
                    if(pos>n||n-pos<4)throw std::runtime_error("incomplete projected text size");uint32_t tag;std::memcpy(&tag,scalars.get(extent.rows+offset+4+pos,4),4);pos+=4;
                    if(text_compressed(tag)){
                        size_t bytes=tag&0x3fffffffu;if(pos>n||n-pos<4+bytes)throw std::runtime_error("incomplete projected compressed text");
                        uint32_t original;std::memcpy(&original,scalars.get(extent.rows+offset+4+pos,4),4);pos+=4;
                        if(keep){auto *data=scalars.get(extent.rows+offset+4+pos,bytes);std::string value(original,'\0');uLongf length=original;if(uncompress(reinterpret_cast<Bytef*>(value.data()),&length,data,bytes)!=Z_OK||length!=original)throw std::runtime_error("invalid scanned compressed text");row[col]=std::move(value);}pos+=bytes;
                    }else{
                        if(tag>n-pos)throw std::runtime_error("incomplete projected text");
                        if(keep){auto *data=scalars.get(extent.rows+offset+4+pos,tag);row[col]=std::string(reinterpret_cast<const char*>(data),tag);}pos+=tag;
                    }
                }
            }
            if(pos!=n)throw std::runtime_error("extra projected scalar bytes");return true;
        }return false;
    }
};
