/* Exact integer screening for supported aligned dimensions; no ANN. */
static uint64_t dynamic_scans=0;
uint64_t mv_dynamic_scans(void){return dynamic_scans;}
int mv_dynamic_available(size_t dim){static int enabled=-1;if(enabled<0){const char *p=getenv("MINIDB_DYNAMIC_BLOCKS");enabled=!p||strcmp(p,"0");}return enabled&&(dim>=16&&dim<=4096&&dim%16==0&&dim!=128)&&mv_quant_available();}
int mv_dynamic_pack_row(const unsigned char *row,size_t dim,unsigned char *tiles,size_t slot,double *maximum_norm,double *maximum_error){
 if(!row||!tiles||!maximum_norm||!maximum_error||!mv_dynamic_available(dim))return 0;
 double largest=0,norm=0,error=0;float values[dim];memcpy(values,row+8,dim*4);
 for(size_t j=0;j<dim;j++){if(!isfinite(values[j]))return 0;largest=fmax(largest,fabs(values[j]));norm+=(double)values[j]*values[j];}
 double scale=largest?largest/127:1;unsigned char *tile=tiles+(slot/16)*(dim+16)*16;size_t lane=slot%16;
 for(size_t j=0;j<dim;j++){int code=(int)nearbyint(values[j]/scale);if(code>127)code=127;if(code<-127)code=-127;int8_t byte=(int8_t)code;memcpy(tile+(j/2)*32+lane*2+j%2,&byte,1);double e=values[j]-code*scale;error+=e*e;}
 memcpy(tile+dim*16+lane*8,&norm,8);memcpy(tile+dim*16+128+lane*8,&scale,8);
 norm=nextafter(sqrt(norm)*(1+1e-12),INFINITY);error=nextafter(sqrt(error)*(1+1e-12),INFINITY);
 if(norm>*maximum_norm)*maximum_norm=norm;if(error>*maximum_error)*maximum_error=error;return 1;
}
static short dynamic_query[4096];static double dynamic_query_norm=0,dynamic_query_scale=0;
__attribute__((target("avx2"),always_inline))
static inline void mv_dynamic_dot_part(const unsigned char *tile,size_t dim,const short *query,int32_t *out){
 __m256i a=_mm256_setzero_si256(),b=a,c=a,d=a;
 for(size_t j=0;j<dim/2;j+=2){
  int32_t w0,w1;memcpy(&w0,query+j*2,4);memcpy(&w1,query+j*2+2,4);__m256i q0=_mm256_set1_epi32(w0),q1=_mm256_set1_epi32(w1);const unsigned char *p=tile+j*32;
  a=_mm256_add_epi32(a,_mm256_madd_epi16(_mm256_cvtepi8_epi16(_mm_loadu_si128((const __m128i*)p)),q0));b=_mm256_add_epi32(b,_mm256_madd_epi16(_mm256_cvtepi8_epi16(_mm_loadu_si128((const __m128i*)(p+16))),q0));
  c=_mm256_add_epi32(c,_mm256_madd_epi16(_mm256_cvtepi8_epi16(_mm_loadu_si128((const __m128i*)(p+32))),q1));d=_mm256_add_epi32(d,_mm256_madd_epi16(_mm256_cvtepi8_epi16(_mm_loadu_si128((const __m128i*)(p+48))),q1));
 }_mm256_storeu_si256((__m256i*)out,_mm256_add_epi32(a,c));_mm256_storeu_si256((__m256i*)(out+8),_mm256_add_epi32(b,d));
}
__attribute__((target("avx2,avx512f,avx512bw"),always_inline))
static inline void mv_dynamic_dot_wide_part(const unsigned char *tile,size_t dim,const short *query,int32_t *out){
 __m512i a=_mm512_setzero_si512(),b=a,c=a,d=a;
 for(size_t j=0;j<dim/2;j+=4){
  int32_t w0,w1,w2,w3;memcpy(&w0,query+j*2,4);memcpy(&w1,query+j*2+2,4);memcpy(&w2,query+j*2+4,4);memcpy(&w3,query+j*2+6,4);const unsigned char *p=tile+j*32;
  a=_mm512_add_epi32(a,_mm512_madd_epi16(_mm512_cvtepi8_epi16(_mm256_loadu_si256((const __m256i*)p)),_mm512_set1_epi32(w0)));b=_mm512_add_epi32(b,_mm512_madd_epi16(_mm512_cvtepi8_epi16(_mm256_loadu_si256((const __m256i*)(p+32))),_mm512_set1_epi32(w1)));
  c=_mm512_add_epi32(c,_mm512_madd_epi16(_mm512_cvtepi8_epi16(_mm256_loadu_si256((const __m256i*)(p+64))),_mm512_set1_epi32(w2)));d=_mm512_add_epi32(d,_mm512_madd_epi16(_mm512_cvtepi8_epi16(_mm256_loadu_si256((const __m256i*)(p+96))),_mm512_set1_epi32(w3)));
 }_mm512_storeu_si512((void*)out,_mm512_add_epi32(_mm512_add_epi32(a,b),_mm512_add_epi32(c,d)));
}
static size_t mv_dynamic_tile_budget(size_t dim){return dim>512?32u<<20:8u<<20;}
__attribute__((target("avx2"),always_inline))
static inline void mv_dynamic_dot(const unsigned char *tile,size_t dim,int32_t *out){mv_dynamic_dot_part(tile,dim,dynamic_query,out);}
__attribute__((target("avx2,avx512f,avx512bw"),always_inline))
static inline void mv_dynamic_dot_wide(const unsigned char *tile,size_t dim,int32_t *out){mv_dynamic_dot_wide_part(tile,dim,dynamic_query,out);}
__attribute__((target("avx2"),always_inline))
static inline void mv_dynamic_dot_long(const unsigned char *tile,size_t dim,double *out){
 __m256d sums[4];for(size_t i=0;i<4;i++)sums[i]=_mm256_setzero_pd();
 for(size_t base=0;base<dim;base+=512){int32_t part[16];size_t count=dim-base<512?dim-base:512;mv_dynamic_dot_part(tile+base*16,count,dynamic_query+base,part);for(size_t i=0;i<4;i++)sums[i]=_mm256_add_pd(sums[i],_mm256_cvtepi32_pd(_mm_loadu_si128((const __m128i*)(part+4*i))));}
 for(size_t i=0;i<4;i++)_mm256_storeu_pd(out+4*i,sums[i]);
}
__attribute__((target("avx2,avx512f,avx512bw"),always_inline))
static inline void mv_dynamic_dot_long_wide(const unsigned char *tile,size_t dim,double *out){
 __m512d a=_mm512_setzero_pd(),b=a;
 for(size_t base=0;base<dim;base+=512){int32_t part[16];size_t count=dim-base<512?dim-base:512;mv_dynamic_dot_wide_part(tile+base*16,count,dynamic_query+base,part);a=_mm512_add_pd(a,_mm512_cvtepi32_pd(_mm256_loadu_si256((const __m256i*)part)));b=_mm512_add_pd(b,_mm512_cvtepi32_pd(_mm256_loadu_si256((const __m256i*)(part+8))));}
 _mm512_storeu_pd(out,a);_mm512_storeu_pd(out+8,b);
}
#define MV_DYN_TARGET __attribute__((target("avx2")))
#define MV_DYN_NAME mv_dynamic_scan
#define MV_DYN_DOT mv_dynamic_dot
#include "block_dynamic_scan.h"
#undef MV_DYN_TARGET
#undef MV_DYN_NAME
#undef MV_DYN_DOT
#define MV_DYN_TARGET __attribute__((target("avx2,avx512f,avx512bw")))
#define MV_DYN_NAME mv_dynamic_scan_wide
#define MV_DYN_DOT mv_dynamic_dot_wide
#include "block_dynamic_scan.h"
#undef MV_DYN_TARGET
#undef MV_DYN_NAME
#undef MV_DYN_DOT
#define MV_DYN_LONG 1
#define MV_DYN_TARGET __attribute__((target("avx2")))
#define MV_DYN_NAME mv_dynamic_long_scan
#define MV_DYN_DOT mv_dynamic_dot_long
#include "block_dynamic_scan.h"
#undef MV_DYN_TARGET
#undef MV_DYN_NAME
#undef MV_DYN_DOT
#define MV_DYN_TARGET __attribute__((target("avx2,avx512f,avx512bw")))
#define MV_DYN_NAME mv_dynamic_long_scan_wide
#define MV_DYN_DOT mv_dynamic_dot_long_wide
#include "block_dynamic_scan.h"
#undef MV_DYN_TARGET
#undef MV_DYN_NAME
#undef MV_DYN_DOT
#undef MV_DYN_LONG
size_t mv_dynamic_topk(const unsigned char *tiles,size_t slots,size_t dim,const double *query,double norm,double encoding_error,const unsigned char *sample,const uint32_t *ranges,size_t range_count,size_t k,uint32_t *selected){
 if(!mv_dynamic_available(dim)||!tiles||!slots||slots%16||slots>mv_dynamic_tile_budget(dim)/(dim+16)||!query||!sample||!ranges||!selected||!k||k>1024)return SIZE_MAX;
 double largest=0,error=0;dynamic_query_norm=0;
 for(size_t j=0;j<dim;j++){if(!isfinite(query[j]))return SIZE_MAX;largest=fmax(largest,fabs(query[j]));dynamic_query_norm+=query[j]*query[j];}
 dynamic_query_scale=largest?largest/32767:1;
 for(size_t j=0;j<dim;j++){double c=nearbyint(query[j]/dynamic_query_scale);if(c>32767)c=32767;if(c<-32767)c=-32767;dynamic_query[j]=(short)c;double e=query[j]-c*dynamic_query_scale;error+=e*e;}
 double qnorm=nextafter(sqrt(dynamic_query_norm)*(1+1e-12),INFINITY);error=nextafter(sqrt(error)*(1+1e-12),INFINITY);
 double delta=nextafter((2*(qnorm*encoding_error+(norm+encoding_error)*error)+1e-10*(norm*norm+dynamic_query_norm))*(1+1e-10),INFINITY);
 double sampled=0;for(size_t j=0;j<dim;j++){float x;memcpy(&x,sample+j*4,4);double d=x-query[j];sampled+=d*d;}if(!isfinite(delta)||delta>sampled*.03)return SIZE_MAX;
 size_t found=dim>512?(mv_block_wide_available()?mv_dynamic_long_scan_wide(tiles,slots,dim,delta,ranges,range_count,k,selected):mv_dynamic_long_scan(tiles,slots,dim,delta,ranges,range_count,k,selected)):mv_block_wide_available()?mv_dynamic_scan_wide(tiles,slots,dim,delta,ranges,range_count,k,selected):mv_dynamic_scan(tiles,slots,dim,delta,ranges,range_count,k,selected);if(found!=SIZE_MAX)++dynamic_scans;return found;
}
