/* Dimension-scaled outward intervals certify ordering only. Ambiguous
   candidates and every reported distance retain PositiveSum reduction. */
__attribute__((target("avx2,fma"),noinline))
static void mv_refine_dynamic_four_avx2(const unsigned char *const *rows,const double *query,size_t dim,double *scores){
 __m256d a=_mm256_setzero_pd(),b=a,c=a,d=a;
 for(size_t j=0;j<dim;j+=4){
  __m256d q=_mm256_loadu_pd(query+j);
  __m256d x0=_mm256_sub_pd(_mm256_cvtps_pd(_mm_loadu_ps((const float*)(rows[0]+j*4))),q);a=_mm256_fmadd_pd(x0,x0,a);
  __m256d x1=_mm256_sub_pd(_mm256_cvtps_pd(_mm_loadu_ps((const float*)(rows[1]+j*4))),q);b=_mm256_fmadd_pd(x1,x1,b);
  __m256d x2=_mm256_sub_pd(_mm256_cvtps_pd(_mm_loadu_ps((const float*)(rows[2]+j*4))),q);c=_mm256_fmadd_pd(x2,x2,c);
  __m256d x3=_mm256_sub_pd(_mm256_cvtps_pd(_mm_loadu_ps((const float*)(rows[3]+j*4))),q);d=_mm256_fmadd_pd(x3,x3,d);
 }
 double lanes[4];
 _mm256_storeu_pd(lanes,a);scores[0]=(lanes[0]+lanes[1])+(lanes[2]+lanes[3]);
 _mm256_storeu_pd(lanes,b);scores[1]=(lanes[0]+lanes[1])+(lanes[2]+lanes[3]);
 _mm256_storeu_pd(lanes,c);scores[2]=(lanes[0]+lanes[1])+(lanes[2]+lanes[3]);
 _mm256_storeu_pd(lanes,d);scores[3]=(lanes[0]+lanes[1])+(lanes[2]+lanes[3]);
}
__attribute__((target("avx2,avx512f,avx512bw"),noinline))
static void mv_refine_dynamic_four_wide(const unsigned char *const *rows,const double *query,size_t dim,double *scores){
 __m512d a=_mm512_setzero_pd(),b=a,c=a,d=a;
 for(size_t j=0;j<dim;j+=8){
  __m512d q=_mm512_loadu_pd(query+j);
  __m512d x0=_mm512_sub_pd(_mm512_cvtps_pd(_mm256_loadu_ps((const float*)(rows[0]+j*4))),q);a=_mm512_fmadd_pd(x0,x0,a);
  __m512d x1=_mm512_sub_pd(_mm512_cvtps_pd(_mm256_loadu_ps((const float*)(rows[1]+j*4))),q);b=_mm512_fmadd_pd(x1,x1,b);
  __m512d x2=_mm512_sub_pd(_mm512_cvtps_pd(_mm256_loadu_ps((const float*)(rows[2]+j*4))),q);c=_mm512_fmadd_pd(x2,x2,c);
  __m512d x3=_mm512_sub_pd(_mm512_cvtps_pd(_mm256_loadu_ps((const float*)(rows[3]+j*4))),q);d=_mm512_fmadd_pd(x3,x3,d);
 }
 double lanes[8];
 _mm512_storeu_pd(lanes,a);scores[0]=((lanes[0]+lanes[1])+(lanes[2]+lanes[3]))+((lanes[4]+lanes[5])+(lanes[6]+lanes[7]));
 _mm512_storeu_pd(lanes,b);scores[1]=((lanes[0]+lanes[1])+(lanes[2]+lanes[3]))+((lanes[4]+lanes[5])+(lanes[6]+lanes[7]));
 _mm512_storeu_pd(lanes,c);scores[2]=((lanes[0]+lanes[1])+(lanes[2]+lanes[3]))+((lanes[4]+lanes[5])+(lanes[6]+lanes[7]));
 _mm512_storeu_pd(lanes,d);scores[3]=((lanes[0]+lanes[1])+(lanes[2]+lanes[3]))+((lanes[4]+lanes[5])+(lanes[6]+lanes[7]));
}
size_t mv_refine_dynamic_candidates(const unsigned char *view,const uint64_t *records,const double *query,size_t dim,size_t k,uint32_t *positions,size_t count,double *scores,int *certified){
 if(!view||!records||!query||!positions||!scores||!certified||dim<16||dim>4096||dim%16||!k||k>1024||!count||count>4096)return SIZE_MAX;
 *certified=0;struct mv_refine_item items[count];size_t first=0;
 static int four_enabled=-1;if(four_enabled<0){const char *flag=getenv("MINIDB_REFINE_FOUR");four_enabled=!flag||strcmp(flag,"0");}
 if(four_enabled&&mv_screen_simd()&&__builtin_cpu_supports("fma")){
  int wide=mv_block_wide_available();
  for(;first+4<=count;first+=4){
   const unsigned char *rows[4];double sums[4];for(size_t j=0;j<4;j++)rows[j]=view+records[positions[first+j]]+8;
   if(wide)mv_refine_dynamic_four_wide(rows,query,dim,sums);else mv_refine_dynamic_four_avx2(rows,query,dim,sums);
   for(size_t j=0;j<4;j++){if(!isfinite(sums[j]))return SIZE_MAX;items[first+j]=(struct mv_refine_item){sums[j],positions[first+j]};}
   ++refine_four_calls;refine_four_points+=4;
  }
 }
 for(size_t i=first;i<count;i++){
  const unsigned char *row=view+records[positions[i]]+8;
  double score=mv_screen_simd()?avx2_distance(row,query,dim,INFINITY):scalar_distance(row,query,dim,INFINITY);
  if(!isfinite(score))return SIZE_MAX;items[i]=(struct mv_refine_item){score,positions[i]};
 }
 qsort(items,count,sizeof(*items),mv_compare_refine);
 double error=64.0*DBL_EPSILON*(dim+1);size_t take=count<k?count:k;
 double upper=nextafter(items[take-1].score*(1+error)+DBL_MIN,INFINITY);
 size_t kept=take;while(kept<count&&nextafter(items[kept].score*(1-error)-DBL_MIN,-INFINITY)<=upper)++kept;
 int certain=kept==take;
 for(size_t i=1;i<take&&certain;i++){
  double previous=nextafter(items[i-1].score*(1+error)+DBL_MIN,INFINITY);
  double next=nextafter(items[i].score*(1-error)-DBL_MIN,-INFINITY);
  if(!(previous<next))certain=0;
 }
 for(size_t i=0;i<kept;i++)positions[i]=items[i].slot;
 if(certain){for(size_t i=0;i<take;i++)scores[i]=items[i].score;*certified=1;}
 return kept;
}
