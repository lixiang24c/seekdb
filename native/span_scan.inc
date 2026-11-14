MV_SPAN_TARGET
static size_t MV_SPAN_NAME(const struct mv_record_span *spans,size_t span_count,
                            size_t dim,const double *query,double bound,size_t k,uint32_t *selected){
    if(!spans||!span_count||!query||!selected||!dim||dim>4096||!k||k>1024||isnan(bound)||bound<0)return SIZE_MAX;
    size_t stride=8+dim*4,count=0,active=0,first=span_count;
    for(size_t s=0;s<span_count;s++){
        if(spans[s].count>((4u<<20)/stride)-count||(!spans[s].data&&spans[s].count))return SIZE_MAX;
        count+=spans[s].count;if(spans[s].count&&first==span_count)first=s;
        if(spans[s].indices)active+=spans[s].index_count;else if(spans[s].mask){for(size_t i=0;i<spans[s].count;i++)active+=spans[s].mask[i]!=0;}else active+=spans[s].count;
    }
    if(!active)return 0;
    distance_fn distance=choose_screen_distance(dim,active,query,spans[first].data,spans[first].count*stride);
#ifdef MV_SPAN_DISPATCH
    if(distance==avx2_fma_distance128&&isfinite(quant_delta)){
        int packed=1;for(size_t i=0;i<span_count;i++)if(!spans[i].quant)packed=0;
        if(packed){++quant_scan_calls;return mv_spans_quant(spans,span_count,dim,query,bound,k,selected);}
    }
    if(distance==avx2_fma_distance128)return mv_spans_fma(spans,span_count,dim,query,bound,k,selected);
#endif
    double distances[count],heap[k];size_t used=0,candidates=0,base=0;int dense=active>4*k;
    double error=float_screen_selected?((dim+15)/16+8)*FLT_EPSILON:64.0*DBL_EPSILON*(dim+1);
    double slack=float_screen_selected?64.0*FLT_TRUE_MIN*(dim+1):DBL_MIN;
#ifdef MV_SPAN_QUANT
    slack=nextafter(slack+quant_delta*(1+error),INFINITY);
#endif
    double cutoff=nextafter(bound*(1+error)+slack,INFINITY),guard=(1+error)/(1-error);
    for(size_t s=0;s<span_count;s++){
        const struct mv_record_span *span=spans+s;
        for(size_t entry=0;entry<(span->indices?span->index_count:span->count);entry++){
            size_t i=span->indices?span->indices[entry]-span->index_base:entry;
            if(span->mask&&!span->mask[i])continue;
#ifdef MV_SPAN_QUANT
            const unsigned char *record=span->quant+i*144;
#else
            const unsigned char *record=span->data+i*stride+8;
#endif
            double sum=MV_SPAN_DISTANCE(record,query,dim,cutoff);if(!isfinite(sum))continue;
            distances[candidates]=sum;selected[candidates++]=(uint32_t)(base+i);
            if(!dense)continue;
            if(used<k){size_t at=used++;while(at&&heap[(at-1)/2]<sum){heap[at]=heap[(at-1)/2];at=(at-1)/2;}heap[at]=sum;}
            else if(sum<heap[0]){size_t at=0;while(at*2+1<used){size_t child=at*2+1;if(child+1<used&&heap[child+1]>heap[child])++child;if(heap[child]<=sum)break;heap[at]=heap[child];at=child;}heap[at]=sum;}
            if(used==k){double local=nextafter(heap[0]*guard+2*slack/(1-error),INFINITY);if(local<cutoff)cutoff=local;}
        }base+=span->count;
    }
    size_t found=0;for(size_t i=0;i<candidates;i++)if(distances[i]<=cutoff)selected[found++]=selected[i];
    if(dense&&float_screen_selected&&found>4*k&&found>active/256){
        broad_dimension=dim;memcpy(broad_query,query,dim*sizeof(double));broad_radius=sqrt(heap[0])*sqrt(error)*.01;
        int enabled=float_screen_enabled;float_screen_enabled=0;
        size_t result=mv_screen_topk_spans(spans,span_count,dim,query,bound,k,selected);float_screen_enabled=enabled;
        if(prepared_query==query&&prepared_dimension==dim){prepared_distance=avx2_distance;prepared_float=0;}return result;
    }
    return found;
}
