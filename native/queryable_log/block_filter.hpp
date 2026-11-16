// Included within LogDatabase. Filters own bitsets in block-slot order;
// they are discarded with the vector layout, including once-only regrouping.
    const BlockVectors::Filter *blocked_filter(const LogTable &table,const unsigned char *view,const std::vector<BoundCondition> &conditions){
        auto &cache=block_vectors;
        // The vector builder reserves this allowance from its original
        // capacity before becoming ready. No procfs read on cache hits.
        size_t budget=cache.filter_budget;if(!budget)return nullptr;
        std::string key;
        for(auto &c:conditions){if(auto text=std::get_if<std::string>(&c.value))if(text->size()>budget)return nullptr;auto part=std::to_string(c.column)+c.op+cell_json(c.value);if(part.size()>=budget||key.size()>=budget-part.size())return nullptr;key+='|';key+=part;}
        auto evict=[&](){cache.filter_bytes-=cache.filters.front().charge;cache.filters.pop_front();};
        while(!cache.filters.empty()&&cache.filter_bytes>budget)evict();
        for(auto it=cache.filters.begin();it!=cache.filters.end();++it)if(it->key==key){cache.filters.splice(cache.filters.end(),cache.filters,it);auto &entry=cache.filters.back();if(!entry.bits.empty()){++block_filter_hits;return &entry;}break;}
        if(cache.filters.empty()||cache.filters.back().key!=key){
            size_t charge=sizeof(BlockVectors::Filter)+48+key.capacity()+1;if(charge>budget)return nullptr;
            while(!cache.filters.empty()&&cache.filter_bytes>budget-charge)evict();
            try{BlockVectors::Filter entry;entry.key=std::move(key);entry.charge=charge;cache.filters.push_back(std::move(entry));cache.filter_bytes+=charge;}catch(const std::bad_alloc&){return nullptr;}
            return nullptr;
        }
        auto &entry=cache.filters.back();if(entry.attempted)return nullptr;entry.attempted=true;
        size_t bytes=(cache.records.size()/16)*sizeof(uint16_t);if(bytes>budget||entry.charge>budget-bytes)return nullptr;
        while(cache.filters.size()>1&&cache.filter_bytes>budget-bytes)evict();
        if(cache.filter_bytes>budget-bytes)return nullptr;
        try{
            std::vector<uint16_t> bits(cache.records.size()/16,0);if(bits.capacity()*sizeof(uint16_t)>budget-cache.filter_bytes)return nullptr;
            LogPredicate predicate(table.schema,conditions);size_t active=0,stride=8+table.schema.dimension()*4;
            for(size_t group=0;group<cache.groups;group++){
                size_t extent=0;
                for(size_t slot=cache.starts[group];slot<size_t(cache.starts[group])+cache.counts[group];slot++){
                    uint64_t point=cache.records[slot]&MV_CODEC_OFFSET;while(extent+1<table.extents.size()&&point>=table.extents[extent+1].vectors+8)++extent;
                    const auto &e=table.extents[extent];stride=e.stride(table.schema.dimension());uint64_t first=e.vectors+8;
                    if(point<first||(point-first)%stride||(point-first)/stride>=e.count)throw std::runtime_error("block filter record outside extent");
                    size_t ordinal=(point-first)/stride;uint64_t relative;read(&relative,8,e.rows+e.keys+16+ordinal*16,view);
                    if(relative<8||relative>e.keys||e.keys-relative<4)throw std::runtime_error("block filter scalar outside extent");
                    uint32_t n;read(&n,4,e.rows+relative,view);if(n>e.keys-relative-4)throw std::runtime_error("block filter scalar exceeds extent");
                    if(predicate.matches(view+e.rows+relative+4,n)){bits[slot/16]|=uint16_t(1u<<(slot%16));++active;}
                }
            }
            entry.bits=std::move(bits);entry.active=active;size_t charge=entry.bits.capacity()*sizeof(uint16_t);entry.charge+=charge;cache.filter_bytes+=charge;
            if(!cache.filter_ready){Bytes().swap(quant_cache);quant_name.clear();quant_uses=0;quant_rejected=false;cache.filter_ready=true;}
            ++block_filter_builds;block_filter_peak=std::max(block_filter_peak,uint64_t(cache.filter_bytes));size_t held=sizeof(BlockVectors)+cache.data.capacity()+cache.records.capacity()*8+cache.bounds.capacity()*16+cache.text_dictionary.capacity()*sizeof(std::string)+cache.text_keys;block_peak=std::max(block_peak,uint64_t(held+cache.filter_bytes));return &entry;
        }catch(const std::bad_alloc&){return nullptr;}
    }
