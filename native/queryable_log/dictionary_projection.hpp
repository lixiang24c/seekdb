// Included within LogDatabase. A compact primary-key interval permits direct
// one-byte column lookup without retaining row objects or a key hash table.
    std::shared_ptr<const DictionaryProjectionData> cached_dictionary_projection(const LogTable &table,size_t column){
        if(multi_owner==&table&&multi_text)for(auto &data:multi_text->columns)if(data&&data->column==column){++projected_hits;return data;}
        if(projected_owner!=&table){clear_dictionary_projection();projected_owner=&table;projected_column=column;}
        if(projected_column!=column)return nullptr;
        if(projected_text){++projected_hits;return projected_text;}
        if(projected_attempted||++projected_requests<2)return nullptr;
        projected_attempted=true;auto data=build_dictionary_projection(table,column,std::min(size_t(64u<<10),size_t(resident_budget/256)));
        if(data){projected_text=std::move(data);++projected_builds;projected_peak=std::max(projected_peak,uint64_t(projected_text->charge));}return projected_text;
    }
    std::shared_ptr<const DictionaryProjectionData> build_dictionary_projection(const LogTable &table,size_t column,size_t budget){
        size_t count=table.schema.count;
        if(!count||budget<=sizeof(DictionaryProjectionData)+64||count>budget-sizeof(DictionaryProjectionData)-64)return nullptr;
        // Bound preparation I/O as well as the retained dictionary. A small
        // row count can still represent huge VARCHAR payloads.
        uint64_t scalar_limit=std::min(uint64_t(4u<<20),resident_budget/32),scalar_bytes=0;
        for(auto &extent:table.extents){if(extent.keys>scalar_limit-scalar_bytes){return nullptr;}scalar_bytes+=extent.keys;}
        auto view=mapped(table);if(!view)return nullptr;size_t physical=0;for(auto &extent:table.extents)physical+=extent.count;
        const TextColumn *texts=nullptr;const LogIntColumn *integers=nullptr;
        if(table.schema.columns[column].type=="VARCHAR"){
            texts=cached_text_column(table,column,physical,view);
            if(!texts&&!text_column.attempted)texts=cached_text_column(table,column,physical,view);
        }else if(table.schema.columns[column].type=="BIGINT"){
            integers=cached_integer_column(table,column,physical,view);
            if(!integers&&!integer_column.attempted)integers=cached_integer_column(table,column,physical,view);
        }
        if(!texts&&!integers)return nullptr;
        const auto &codes=texts?texts->codes:integers->codes;size_t distinct=texts?texts->dictionary.size():integers->distinct;
        try{
            int64_t minimum=INT64_MAX,maximum=INT64_MIN;size_t seen=0;
            for(auto &e:table.extents)for(size_t i=0;i<e.count;i++)if(e.visible(i)){int64_t id;read(&id,8,e.rows+e.keys+8+i*16,view);minimum=std::min(minimum,id);maximum=std::max(maximum,id);++seen;}
            // A compact key domain permits a direct map with a presence
            // bitmap for holes. The same total allowance bounds both arrays.
            if(seen!=count)return nullptr;uint64_t span=uint64_t(maximum)-uint64_t(minimum);
            if(span>=budget)return nullptr;size_t slots=size_t(span)+1,holder=sizeof(DictionaryProjectionData)+64;
            if(slots>budget-holder||(slots!=count&&(slots+7)/8>budget-holder-slots))return nullptr;
            auto data=std::make_shared<DictionaryProjectionData>();data->column=column;data->minimum=minimum;data->charge=sizeof(DictionaryProjectionData)+64;
            data->codes.resize(slots);data->charge+=data->codes.capacity();if(data->charge>budget)return nullptr;
            if(slots!=count){size_t bytes=(slots+7)/8;if(bytes>budget-data->charge)return nullptr;data->present.resize(bytes,0);data->charge+=data->present.capacity();if(data->charge>budget)return nullptr;}
            if(distinct>(budget-data->charge)/sizeof(std::string))return nullptr;
            data->json_values.reserve(distinct);data->charge+=data->json_values.capacity()*sizeof(std::string);if(data->charge>budget)return nullptr;
            for(size_t i=0;i<distinct;i++){
                if(texts){auto &value=texts->dictionary[i];if(budget-data->charge<3||value.size()>(budget-data->charge-3)/6)return nullptr;data->json_values.push_back(quote(value));}
                else data->json_values.push_back(std::to_string(integers->dictionary[i]));
                data->charge+=data->json_values.back().capacity()+1;if(data->charge>budget)return nullptr;data->maximum=std::max(data->maximum,data->json_values.back().size());
            }
            size_t base=0;
            for(auto &e:table.extents){for(size_t i=0;i<e.count;i++)if(e.visible(i)){int64_t id;read(&id,8,e.rows+e.keys+8+i*16,view);uint64_t position=uint64_t(id)-uint64_t(minimum);auto code=codes[base+i];if(position>=slots||code>=data->json_values.size())throw std::runtime_error("invalid compact projected column");if(!data->present.empty()){auto &bits=data->present[position/8];unsigned flag=1u<<(position%8);if(bits&flag)throw std::runtime_error("duplicate projected key");bits|=flag;}data->codes[position]=code;}base+=e.count;}
            return data;
        }catch(const std::bad_alloc&){return nullptr;}
    }

    std::shared_ptr<const MultiProjectionData> cached_multi_projection(const LogTable &table,const CompiledProjection &projection){
        if(!projection.multi_count)return nullptr;
        if(multi_owner!=&table){multi_text.reset();multi_owner=&table;multi_columns=projection.multi_columns;multi_requests=0;multi_attempted=false;}
        if(multi_columns!=projection.multi_columns)return nullptr;
        if(multi_text){++projected_hits;return multi_text;}
        if(multi_attempted||++multi_requests<2)return nullptr;multi_attempted=true;
        size_t budget=std::min(size_t(64u<<10),size_t(resident_budget/256));
        if(budget<=sizeof(MultiProjectionData)+64)return nullptr;
        try{
            auto data=std::make_shared<MultiProjectionData>();data->charge=sizeof(MultiProjectionData)+64;
            // Share an existing single-column snapshot rather than evicting
            // it. Charge any extra retained single column to this allowance.
            auto single=projected_owner==&table?projected_text:nullptr;
            if(projected_owner!=&table)projected_text.reset();
            bool included=false;for(size_t i=0;i<projection.multi_count;i++)included=included||(single&&single->column==projection.multi_columns[i]);
            if(single&&!included){if(single->charge>budget-data->charge)return nullptr;data->charge+=single->charge;}
            for(size_t i=0;i<projection.multi_count;i++){
                auto column=single&&single->column==projection.multi_columns[i]?single:build_dictionary_projection(table,projection.multi_columns[i],budget-data->charge);
                if(!column||column->charge>budget-data->charge)return nullptr;
                data->columns[i]=std::move(column);data->charge+=data->columns[i]->charge;
            }
            if(!projected_text){projected_text=data->columns[0];projected_owner=&table;projected_column=projection.multi_columns[0];projected_requests=0;projected_attempted=true;}
            multi_text=std::move(data);++projected_builds;projected_peak=std::max(projected_peak,uint64_t(multi_text->charge));return multi_text;
        }catch(const std::bad_alloc&){return nullptr;}
    }
