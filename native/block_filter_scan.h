/* Included once per supported SIMD target. */
MV_BLOCK_TARGET
static size_t MV_BLOCK_NAME(const unsigned char *tiles,size_t slots,const uint16_t *masks,size_t k,uint32_t *selected){
 if(!tiles||!selected||!masks||!slots||slots%16||slots>(4u<<20)/144||!k||k>1024||!isfinite(quant_delta))return SIZE_MAX;
 size_t active=0;for(size_t tile=0;tile<slots/16;tile++)active+=__builtin_popcount((unsigned)masks[tile]);
 if(!active)return 0;
 const char *tile_flag=getenv("MINIDB_TILE_MASK");int tile_mask=!tile_flag||strcmp(tile_flag,"0");
 double distances[slots],heap[k];size_t used=0,candidates=0;int dense=active>4*k;
 int fp64=mv_fp64_integer_bounds();double error=fp64?64.0*DBL_EPSILON*129:16*FLT_EPSILON,absolute_slack=fp64?64.0*DBL_MIN*129:64.0*FLT_TRUE_MIN*129,slack=nextafter(absolute_slack+quant_delta*(1+error),INFINITY);
 double cutoff=INFINITY,guard=(1+error)/(1-error);
 {
  for(size_t slot=0;slot<slots;slot+=16){
   unsigned mask=masks[slot/16];if(!mask)continue;
   const unsigned char *tile=tiles+(slot/16)*2304;int32_t dots[16];MV_BLOCK_DOT(tile,dots);
   double scores[16];
   MV_BLOCK_SCORES(tile,dots,scores);
   if(tile_mask){
    unsigned eligible=MV_BLOCK_MASK(scores,cutoff);
    mask&=eligible;
   }
   while(mask){size_t lane=(size_t)__builtin_ctz(mask);mask&=mask-1;
    double sum=scores[lane];
    if(!isfinite(sum)||sum>cutoff)continue;
    distances[candidates]=sum;selected[candidates++]=(uint32_t)(slot+lane);
    if(!dense)continue;
    if(used<k){size_t at=used++;while(at&&heap[(at-1)/2]<sum){heap[at]=heap[(at-1)/2];at=(at-1)/2;}heap[at]=sum;}
    else if(sum<heap[0]){size_t at=0;while(at*2+1<used){size_t child=at*2+1;if(child+1<used&&heap[child+1]>heap[child])++child;if(heap[child]<=sum)break;heap[at]=heap[child];at=child;}heap[at]=sum;}
    if(used==k){double local=nextafter(heap[0]*guard+2*slack/(1-error),INFINITY);if(local<cutoff)cutoff=local;}
   }
  }
 }
 size_t found=0;for(size_t i=0;i<candidates;i++)if(distances[i]<=cutoff)selected[found++]=selected[i];
 return found;
}
