#define _POSIX_C_SOURCE 200809L
#include "vector_codec.h"
/* Screening and reference terms; Python resolves inexact sums and ranking. */
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <float.h>
#include <math.h>
#include <stdlib.h>
#include <immintrin.h>
#include <cpuid.h>

unsigned mv_screen_abi(void) { return 5; }

/* Emit the same binary64 products as Python (x-y)*(x-y), without FMA or
   fast-math. Return 0 on invalid input, 1 for terms requiring fsum, or
   (checked entry only) 2 when terms[0] holds their exactly representable sum. */
static int squared_terms(const unsigned char *data, size_t bytes, size_t dim,
                         const double *query, size_t position, double *terms,
                         int checked) {
    if (!data || !query || !terms || !dim || dim > 4096) return 0;
    size_t stride = 8 + dim * 4;
    if (bytes % stride || position >= bytes / stride) return 0;
    const unsigned char *vector = data + position * stride + 8;
    double sum = 0;
    int exact = checked;
    for (size_t j = 0; j < dim; ++j) {
        float value; memcpy(&value, vector + j * 4, 4);
        if (!isfinite(value) || !isfinite(query[j])) return 0;
        double delta = (double)value - query[j];
        double product = delta * delta;
        if (!isfinite(product)) return 0;
        terms[j] = product;
        if (exact) {
            /* FastTwoSum: high >= low >= 0. For finite total, Sterbenz's
               lemma makes total-high exact. Equality with low therefore
               proves this addition lost no bits. Induction proves the
               final sum is exact; any failed step retains terms for fsum.
               Overflow also fails the equality. */
            double high = sum >= product ? sum : product;
            double low = sum >= product ? product : sum;
            double total = high + low;
            if (low != total - high) exact = 0;
            sum = total;
        }
    }
    if (exact) { terms[0] = sum; return 2; }
    return 1;
}

int mv_squared_terms(const unsigned char *data, size_t bytes, size_t dim,
                     const double *query, size_t position, double *terms) {
    return squared_terms(data, bytes, dim, query, position, terms, 0);
}

int mv_squared_checked(const unsigned char *data, size_t bytes, size_t dim,
                       const double *query, size_t position, double *terms) {
    return squared_terms(data, bytes, dim, query, position, terms, 1);
}

static int simd_enabled = 1;
void mv_screen_set_simd(int enabled) { simd_enabled = enabled != 0; }
unsigned mv_screen_simd(void) {
    return simd_enabled && __builtin_cpu_supports("avx2");
}

static double scalar_distance(const unsigned char *vector, const double *query,
                              size_t dim, double cutoff) {
    double sum = 0;
    for (size_t j = 0; j < dim; ++j) {
        float value; memcpy(&value, vector + j * 4, 4);
        double delta = (double)value - query[j];
        sum += delta * delta;
        if (sum > cutoff) return INFINITY;
    }
    return sum;
}

__attribute__((target("avx2"), noinline))
static double avx2_distance(const unsigned char *vector, const double *query,
                            size_t dim, double cutoff) {
    __m256d a = _mm256_setzero_pd(), b = a, c = a, d = a;
    double lanes[4], sum;
    size_t j = 0;
    for (; j + 16 <= dim; j += 16) {
        __m256d x = _mm256_sub_pd(_mm256_cvtps_pd(_mm_loadu_ps((const float *)(vector + 4*j))), _mm256_loadu_pd(query+j));
        __m256d y = _mm256_sub_pd(_mm256_cvtps_pd(_mm_loadu_ps((const float *)(vector + 4*(j+4)))), _mm256_loadu_pd(query+j+4));
        __m256d z = _mm256_sub_pd(_mm256_cvtps_pd(_mm_loadu_ps((const float *)(vector + 4*(j+8)))), _mm256_loadu_pd(query+j+8));
        __m256d w = _mm256_sub_pd(_mm256_cvtps_pd(_mm_loadu_ps((const float *)(vector + 4*(j+12)))), _mm256_loadu_pd(query+j+12));
        a = _mm256_add_pd(a, _mm256_mul_pd(x,x));
        b = _mm256_add_pd(b, _mm256_mul_pd(y,y));
        c = _mm256_add_pd(c, _mm256_mul_pd(z,z));
        d = _mm256_add_pd(d, _mm256_mul_pd(w,w));
        /* Short vectors finish without repeated horizontal reductions.
           Long vectors check a positive partial sum every 64 coordinates. */
        if (dim > 256 && ((j + 16) & 63) == 0) {
            _mm256_storeu_pd(lanes, _mm256_add_pd(_mm256_add_pd(a,b), _mm256_add_pd(c,d)));
            sum = (lanes[0]+lanes[1])+(lanes[2]+lanes[3]);
            if (sum > cutoff) return INFINITY;
        }
    }
    for (; j + 4 <= dim; j += 4) {
        __m256d x = _mm256_sub_pd(_mm256_cvtps_pd(_mm_loadu_ps((const float *)(vector + 4*j))), _mm256_loadu_pd(query+j));
        a = _mm256_add_pd(a, _mm256_mul_pd(x,x));
    }
    _mm256_storeu_pd(lanes, _mm256_add_pd(_mm256_add_pd(a,b), _mm256_add_pd(c,d)));
    sum = (lanes[0]+lanes[1])+(lanes[2]+lanes[3]);
    for (; j < dim; ++j) {
        float value; memcpy(&value, vector + 4*j, 4);
        double delta = (double)value - query[j]; sum += delta * delta;
    }
    return sum > cutoff ? INFINITY : sum;
}

/* Experimental float32 screening only; final ranking remains exact. */
static int float_screen_enabled=0, float_screen_selected=0;
static float float_query[4096];
static double broad_query[256];
static size_t broad_dimension=0;
static double broad_radius=0;
void mv_screen_set_float(int enabled){float_screen_enabled=enabled!=0;broad_dimension=0;}
__attribute__((target("avx2"), noinline))
static double avx2_float_distance(const unsigned char *vector,const double *query,size_t dim,double cutoff){
    __m256 a=_mm256_setzero_ps(),b=a;size_t j=0;
    for(;j+16<=dim;j+=16){
        __m256 x=_mm256_sub_ps(_mm256_loadu_ps((const float*)(vector+j*4)),_mm256_loadu_ps(float_query+j));
        __m256 y=_mm256_sub_ps(_mm256_loadu_ps((const float*)(vector+(j+8)*4)),_mm256_loadu_ps(float_query+j+8));
        a=_mm256_add_ps(a,_mm256_mul_ps(x,x));b=_mm256_add_ps(b,_mm256_mul_ps(y,y));
    }
    /* Convert float lanes before reducing: binary64 addition keeps the
       existing conservative screening bound, with a shorter dependency chain. */
    __m256 total=_mm256_add_ps(a,b);
    __m256d pairs=_mm256_add_pd(_mm256_cvtps_pd(_mm256_castps256_ps128(total)),_mm256_cvtps_pd(_mm256_extractf128_ps(total,1)));
    __m128d halves=_mm_add_pd(_mm256_castpd256_pd128(pairs),_mm256_extractf128_pd(pairs,1));
    double sum=_mm_cvtsd_f64(_mm_add_sd(halves,_mm_unpackhi_pd(halves,halves)));
    for(;j<dim;j++){float value;memcpy(&value,vector+j*4,4);double delta=(double)value-query[j];sum+=delta*delta;}
    if(!isfinite(sum))return avx2_distance(vector,query,dim,cutoff);
    return sum>cutoff?INFINITY:sum;
}

/* Fixed-width dispatch retains the generic arithmetic order and guard. */
__attribute__((target("avx2"), noinline))
static double avx2_float_distance128(const unsigned char *vector,const double *query,size_t ignored_dimension,double cutoff){
    __m256 a=_mm256_setzero_ps(),b=a;size_t j=0;
    for(;j<128;j+=16){
        __m256 x=_mm256_sub_ps(_mm256_loadu_ps((const float*)(vector+j*4)),_mm256_loadu_ps(float_query+j));
        __m256 y=_mm256_sub_ps(_mm256_loadu_ps((const float*)(vector+(j+8)*4)),_mm256_loadu_ps(float_query+j+8));
        a=_mm256_add_ps(a,_mm256_mul_ps(x,x));b=_mm256_add_ps(b,_mm256_mul_ps(y,y));
    }
    /* Convert float lanes before reducing: binary64 addition keeps the
       existing conservative screening bound, with a shorter dependency chain. */
    __m256 total=_mm256_add_ps(a,b);
    __m256d pairs=_mm256_add_pd(_mm256_cvtps_pd(_mm256_castps256_ps128(total)),_mm256_cvtps_pd(_mm256_extractf128_ps(total,1)));
    __m128d halves=_mm_add_pd(_mm256_castpd256_pd128(pairs),_mm256_extractf128_pd(pairs,1));
    double sum=_mm_cvtsd_f64(_mm_add_sd(halves,_mm_unpackhi_pd(halves,halves)));
    if(!isfinite(sum))return avx2_distance(vector,query,128,cutoff);
    return sum>cutoff?INFINITY:sum;
}

/* Four accumulation chains for conservative float screening only. The
   existing bound includes more rounding steps than this fused variant. Final
   distances and primary-key ordering still use the original exact reducer. */
__attribute__((target("avx2,fma"), always_inline))
static inline double avx2_fma_distance128(const unsigned char *vector,const double *query,size_t ignored,double cutoff){
    __m256 a=_mm256_setzero_ps(),b=a,c=a,d=a;
    #pragma GCC unroll 4
    for(size_t j=0;j<128;j+=32){
        __m256 x=_mm256_sub_ps(_mm256_loadu_ps((const float*)(vector+j*4)),_mm256_loadu_ps(float_query+j));
        __m256 y=_mm256_sub_ps(_mm256_loadu_ps((const float*)(vector+(j+8)*4)),_mm256_loadu_ps(float_query+j+8));
        __m256 z=_mm256_sub_ps(_mm256_loadu_ps((const float*)(vector+(j+16)*4)),_mm256_loadu_ps(float_query+j+16));
        __m256 w=_mm256_sub_ps(_mm256_loadu_ps((const float*)(vector+(j+24)*4)),_mm256_loadu_ps(float_query+j+24));
        a=_mm256_fmadd_ps(x,x,a);b=_mm256_fmadd_ps(y,y,b);c=_mm256_fmadd_ps(z,z,c);d=_mm256_fmadd_ps(w,w,d);
    }
    __m256 total=_mm256_add_ps(_mm256_add_ps(a,b),_mm256_add_ps(c,d));
    __m256d pairs=_mm256_add_pd(_mm256_cvtps_pd(_mm256_castps256_ps128(total)),_mm256_cvtps_pd(_mm256_extractf128_ps(total,1)));
    __m128d halves=_mm_add_pd(_mm256_castpd256_pd128(pairs),_mm256_extractf128_pd(pairs,1));
    double sum=_mm_cvtsd_f64(_mm_add_sd(halves,_mm_unpackhi_pd(halves,halves)));
    if(!isfinite(sum))return avx2_distance(vector,query,128,cutoff);return sum>cutoff?INFINITY:sum;
}

__attribute__((target("avx2"),always_inline)) static inline int32_t dot8(const signed char *x,const short *q){
 __m256i a=_mm256_setzero_si256(),b=a,c=a,d=a;
 #pragma GCC unroll 2
 for(int j=0;j<128;j+=64){
  a=_mm256_add_epi32(a,_mm256_madd_epi16(_mm256_cvtepi8_epi16(_mm_loadu_si128((const __m128i*)(x+j))),_mm256_loadu_si256((const __m256i*)(q+j))));
  b=_mm256_add_epi32(b,_mm256_madd_epi16(_mm256_cvtepi8_epi16(_mm_loadu_si128((const __m128i*)(x+j+16))),_mm256_loadu_si256((const __m256i*)(q+j+16))));
  c=_mm256_add_epi32(c,_mm256_madd_epi16(_mm256_cvtepi8_epi16(_mm_loadu_si128((const __m128i*)(x+j+32))),_mm256_loadu_si256((const __m256i*)(q+j+32))));
  d=_mm256_add_epi32(d,_mm256_madd_epi16(_mm256_cvtepi8_epi16(_mm_loadu_si128((const __m128i*)(x+j+48))),_mm256_loadu_si256((const __m256i*)(q+j+48))));
 }
 __m256i t=_mm256_add_epi32(_mm256_add_epi32(a,b),_mm256_add_epi32(c,d));__m128i s=_mm_add_epi32(_mm256_castsi256_si128(t),_mm256_extracti128_si256(t,1));s=_mm_hadd_epi32(s,s);s=_mm_hadd_epi32(s,s);return _mm_cvtsi128_si32(s);
}
/* Auxiliary quantization only: authoritative vectors and final ranking remain
   float32/binary64. |dot(x,q)-dot(xhat,qhat)| <= ||q|| Ex+(||x||+Ex) Eq.
   Integer dot accumulates at most 128*127*32767, safely within int32. */
static double quant_delta=0,quant_query_norm=0,quant_query_scale=0,quant_query_length=0,quant_query_error=0;
static uint64_t quant_scan_calls=0;
uint64_t mv_quant_scan_calls(void){return quant_scan_calls;}
static short quant_query[128];
__attribute__((target("avx2"),always_inline))
static inline double quant_distance128(const unsigned char *vector,const double *query,size_t ignored,double cutoff){
 double norm,scale;memcpy(&norm,vector+128,8);memcpy(&scale,vector+136,8);
 double sum=norm+quant_query_norm-2*scale*quant_query_scale*dot8((const signed char*)vector,quant_query);
 if(sum<0)sum=0;return sum>cutoff?INFINITY:sum;
}
int mv_quant_available(void){return float_screen_enabled&&mv_screen_simd()&&(_mm_getcsr()&0x8040)==0;}
int mv_quant_pack(const unsigned char *rows,size_t count,unsigned char *packed,double *maxnorm,double *maxerror){
 if(!mv_quant_available())return 0;
 for(size_t i=0;i<count;i++){
  float x[128];memcpy(x,rows+i*520+8,512);double largest=0,norm=0,error=0;
  for(size_t j=0;j<128;j++){if(!isfinite(x[j]))return 0;if(fabs(x[j])>largest)largest=fabs(x[j]);norm+=(double)x[j]*x[j];}
  double scale=largest?largest/127:1;
  for(size_t j=0;j<128;j++){int value=(int)nearbyint(x[j]/scale);if(value>127)value=127;if(value<-127)value=-127;((signed char*)packed)[i*144+j]=(signed char)value;double e=x[j]-value*scale;error+=e*e;}
  memcpy(packed+i*144+128,&norm,8);memcpy(packed+i*144+136,&scale,8);
  norm=nextafter(sqrt(norm)*(1+1e-12),INFINITY);error=nextafter(sqrt(error)*(1+1e-12),INFINITY);
  if(norm>*maxnorm)*maxnorm=norm;if(error>*maxerror)*maxerror=error;
 }return 1;
}
int mv_quant_prepare(const double *q,double norm,double error,const unsigned char *sample){
 double largest=0,qe=0;quant_query_norm=0;
 for(size_t j=0;j<128;j++){if(!isfinite(q[j]))return 0;if(fabs(q[j])>largest)largest=fabs(q[j]);quant_query_norm+=q[j]*q[j];}
 quant_query_scale=largest?largest/32767:1;
 for(size_t j=0;j<128;j++){double rounded=nearbyint(q[j]/quant_query_scale);if(rounded>32767)rounded=32767;if(rounded<-32767)rounded=-32767;quant_query[j]=(short)rounded;double e=q[j]-quant_query[j]*quant_query_scale;qe+=e*e;}
 double qnorm=nextafter(sqrt(quant_query_norm)*(1+1e-12),INFINITY);qe=nextafter(sqrt(qe)*(1+1e-12),INFINITY);
 quant_query_length=qnorm;quant_query_error=qe;
 quant_delta=nextafter((2*(qnorm*error+(norm+error)*qe)+1e-10*(norm*norm+quant_query_norm))*(1+1e-10),INFINITY);
 double sampled=0;for(size_t j=0;j<128;j++){float x;memcpy(&x,sample+j*4,4);double d=(double)x-q[j];sampled+=d*d;}
 return isfinite(quant_delta)&&quant_delta<=sampled*.03;
}

/* Sixteen vectors per tile, adjacent dimension pairs per SIMD lane.
   Same int8/int16 products as dot8; no loss of precision from transposition. */
int mv_quant_pack_block_row(const unsigned char *row,unsigned char *tiles,size_t slot,double *norm,double *error){
 unsigned char packed[144];if(!mv_quant_pack(row,1,packed,norm,error))return 0;
 unsigned char *tile=tiles+(slot/16)*2304;size_t lane=slot%16;
 for(size_t j=0;j<64;j++)memcpy(tile+j*32+lane*2,packed+j*2,2);
 memcpy(tile+2048+lane*8,packed+128,8);memcpy(tile+2176+lane*8,packed+136,8);return 1;
}
__attribute__((target("avx2"),always_inline))
static inline void mv_quant_dot16(const unsigned char *tile,int32_t *out){
 __m256i a=_mm256_setzero_si256(),b=a,c=a,d=a;
 for(size_t j=0;j<64;j+=2){
  int x,y;memcpy(&x,quant_query+2*j,4);memcpy(&y,quant_query+2*j+2,4);
  __m256i u=_mm256_set1_epi32(x),v=_mm256_set1_epi32(y);const unsigned char *p=tile+j*32;
  a=_mm256_add_epi32(a,_mm256_madd_epi16(_mm256_cvtepi8_epi16(_mm_loadu_si128((const __m128i*)p)),u));
  b=_mm256_add_epi32(b,_mm256_madd_epi16(_mm256_cvtepi8_epi16(_mm_loadu_si128((const __m128i*)(p+16))),u));
  c=_mm256_add_epi32(c,_mm256_madd_epi16(_mm256_cvtepi8_epi16(_mm_loadu_si128((const __m128i*)(p+32))),v));
  d=_mm256_add_epi32(d,_mm256_madd_epi16(_mm256_cvtepi8_epi16(_mm_loadu_si128((const __m128i*)(p+48))),v));
 }
 _mm256_storeu_si256((__m256i*)out,_mm256_add_epi32(a,c));
 _mm256_storeu_si256((__m256i*)(out+8),_mm256_add_epi32(b,d));
}
static int mv_block_wide_available(void){
 static int cached=-1;if(cached>=0)return cached;
 const char *flag=getenv("MINIDB_WIDE_SCAN");if(flag&&!strcmp(flag,"0"))return cached=0;
 if(!__builtin_cpu_supports("avx512f")||!__builtin_cpu_supports("avx512bw"))return cached=0;
 unsigned a,b,c,d;if(!__get_cpuid(1,&a,&b,&c,&d)||!(c&bit_OSXSAVE))return cached=0;
 unsigned lo,hi;__asm__ volatile("xgetbv":"=a"(lo),"=d"(hi):"c"(0));
 return cached=(lo&0xe6)==0xe6;
}
const char *mv_block_kernel(void){return mv_block_wide_available()?"avx512bw":"avx2";}
__attribute__((target("avx2,avx512f,avx512bw"),always_inline))
static inline void mv_quant_dot16_wide(const unsigned char *tile,int32_t *out){
 __m512i a=_mm512_setzero_si512(),b=a,c=a,d=a;
 for(size_t j=0;j<64;j+=4){
  int q0,q1,q2,q3;memcpy(&q0,quant_query+2*j,4);memcpy(&q1,quant_query+2*j+2,4);memcpy(&q2,quant_query+2*j+4,4);memcpy(&q3,quant_query+2*j+6,4);
  const unsigned char *p=tile+j*32;
  a=_mm512_add_epi32(a,_mm512_madd_epi16(_mm512_cvtepi8_epi16(_mm256_loadu_si256((const __m256i*)p)),_mm512_set1_epi32(q0)));
  b=_mm512_add_epi32(b,_mm512_madd_epi16(_mm512_cvtepi8_epi16(_mm256_loadu_si256((const __m256i*)(p+32))),_mm512_set1_epi32(q1)));
  c=_mm512_add_epi32(c,_mm512_madd_epi16(_mm512_cvtepi8_epi16(_mm256_loadu_si256((const __m256i*)(p+64))),_mm512_set1_epi32(q2)));
  d=_mm512_add_epi32(d,_mm512_madd_epi16(_mm512_cvtepi8_epi16(_mm256_loadu_si256((const __m256i*)(p+96))),_mm512_set1_epi32(q3)));
 }
 _mm512_storeu_si512((void*)out,_mm512_add_epi32(_mm512_add_epi32(a,b),_mm512_add_epi32(c,d)));
}
__attribute__((target("avx2"),always_inline))
static inline void mv_block_scores_avx2(const unsigned char *tile,const int32_t *dots,double *scores){
   for(size_t lane=0;lane<16;lane+=4){
    __m256d norm=_mm256_loadu_pd((const double*)(tile+2048+lane*8)),scale=_mm256_loadu_pd((const double*)(tile+2176+lane*8));
    __m256d product=_mm256_mul_pd(_mm256_mul_pd(_mm256_set1_pd(2),scale),_mm256_set1_pd(quant_query_scale));
    product=_mm256_mul_pd(product,_mm256_cvtepi32_pd(_mm_loadu_si128((const __m128i*)(dots+lane))));
    __m256d sum=_mm256_sub_pd(_mm256_add_pd(norm,_mm256_set1_pd(quant_query_norm)),product);
    _mm256_storeu_pd(scores+lane,_mm256_max_pd(_mm256_setzero_pd(),sum));
   }
}
__attribute__((target("avx2,avx512f,avx512bw"),always_inline))
static inline void mv_block_scores_wide(const unsigned char *tile,const int32_t *dots,double *scores){
 for(size_t lane=0;lane<16;lane+=8){
  __m512d norm=_mm512_loadu_pd((const double*)(tile+2048+lane*8)),scale=_mm512_loadu_pd((const double*)(tile+2176+lane*8));
  __m512d product=_mm512_mul_pd(_mm512_mul_pd(_mm512_set1_pd(2),scale),_mm512_set1_pd(quant_query_scale));
  product=_mm512_mul_pd(product,_mm512_cvtepi32_pd(_mm256_loadu_si256((const __m256i*)(dots+lane))));
  __m512d sum=_mm512_sub_pd(_mm512_add_pd(norm,_mm512_set1_pd(quant_query_norm)),product);
  _mm512_storeu_pd(scores+lane,_mm512_max_pd(_mm512_setzero_pd(),sum));
 }
}
__attribute__((target("avx2"),always_inline))
static inline unsigned mv_block_mask_avx2(const double *scores,double cutoff){
 unsigned result=0;for(size_t lane=0;lane<16;lane+=4)result|=(unsigned)_mm256_movemask_pd(_mm256_cmp_pd(_mm256_loadu_pd(scores+lane),_mm256_set1_pd(cutoff),_CMP_LE_OQ))<<lane;return result;
}
__attribute__((target("avx2,avx512f,avx512bw"),always_inline))
static inline unsigned mv_block_mask_wide(const double *scores,double cutoff){
 return (unsigned)_mm512_cmp_pd_mask(_mm512_loadu_pd(scores),_mm512_set1_pd(cutoff),_CMP_LE_OQ)|((unsigned)_mm512_cmp_pd_mask(_mm512_loadu_pd(scores+8),_mm512_set1_pd(cutoff),_CMP_LE_OQ)<<8);
}
static unsigned char sparse_pairs[16];static size_t sparse_pair_count=0;
static uint64_t sparse_scan_calls=0,sparse_zero_calls=0,sparse_pair_total=0;
uint64_t mv_sparse_scan_calls(void){return sparse_scan_calls;}
uint64_t mv_sparse_zero_calls(void){return sparse_zero_calls;}
uint64_t mv_sparse_pair_total(void){return sparse_pair_total;}
static int mv_prepare_sparse_query(void){
 static int enabled=-1;if(enabled<0){const char *flag=getenv("MINIDB_SPARSE_QUERY");enabled=!flag||strcmp(flag,"0");}if(!enabled)return 0;
 size_t count=0;for(size_t j=0;j<64;j++){int32_t pair;memcpy(&pair,quant_query+2*j,4);if(pair){if(count==16)return 0;sparse_pairs[count++]=(unsigned char)j;}}
 sparse_pair_count=count;++sparse_scan_calls;sparse_pair_total+=count;if(!count)++sparse_zero_calls;return 1;
}
__attribute__((target("avx2"),always_inline))
static inline void mv_quant_dot16_sparse(const unsigned char *tile,int32_t *out){
 __m256i a=_mm256_setzero_si256(),b=a;
 for(size_t i=0;i<sparse_pair_count;i++){
  size_t j=sparse_pairs[i];int32_t word;memcpy(&word,quant_query+2*j,4);__m256i q=_mm256_set1_epi32(word);const unsigned char *p=tile+j*32;
  a=_mm256_add_epi32(a,_mm256_madd_epi16(_mm256_cvtepi8_epi16(_mm_loadu_si128((const __m128i*)p)),q));
  b=_mm256_add_epi32(b,_mm256_madd_epi16(_mm256_cvtepi8_epi16(_mm_loadu_si128((const __m128i*)(p+16))),q));
 }
 _mm256_storeu_si256((__m256i*)out,a);_mm256_storeu_si256((__m256i*)(out+8),b);
}
__attribute__((target("avx2,avx512f,avx512bw"),always_inline))
static inline void mv_quant_dot16_sparse_wide(const unsigned char *tile,int32_t *out){
 __m512i a=_mm512_setzero_si512();
 for(size_t i=0;i<sparse_pair_count;i++){
  size_t j=sparse_pairs[i];int32_t word;memcpy(&word,quant_query+2*j,4);const unsigned char *p=tile+j*32;
  a=_mm512_add_epi32(a,_mm512_madd_epi16(_mm512_cvtepi8_epi16(_mm256_loadu_si256((const __m256i*)p)),_mm512_set1_epi32(word)));
 }
 _mm512_storeu_si512((void*)out,a);
}
static uint64_t fp64_bound_calls=0,legacy_bound_calls=0;
uint64_t mv_fp64_bound_calls(void){return fp64_bound_calls;}
uint64_t mv_legacy_bound_calls(void){return legacy_bound_calls;}
/* Block scores use exact integer dot products and binary64 norm/scale
   arithmetic. The existing 1e-10*(norm^2+query_norm) term already exceeds
   binary64 arithmetic and reconstruction-rounding errors for 128 float32
   coordinates. Do not inherit the float32 accumulation floor of raw screens. */
static int mv_fp64_integer_bounds(void){static int enabled=-1;if(enabled<0){const char *flag=getenv("MINIDB_F64_QUANT_BOUNDS");enabled=!flag||strcmp(flag,"0");}if(enabled)++fp64_bound_calls;else ++legacy_bound_calls;return enabled;}
#define MV_BLOCK_TARGET __attribute__((target("avx2")))
#define MV_BLOCK_NAME mv_sparse_blocks_avx2
#define MV_BLOCK_DOT mv_quant_dot16_sparse
#define MV_BLOCK_SCORES mv_block_scores_avx2
#define MV_BLOCK_MASK mv_block_mask_avx2
#include "block_scan_impl.h"
#undef MV_BLOCK_TARGET
#undef MV_BLOCK_NAME
#undef MV_BLOCK_DOT
#undef MV_BLOCK_SCORES
#undef MV_BLOCK_MASK
#define MV_BLOCK_TARGET __attribute__((target("avx2,avx512f,avx512bw")))
#define MV_BLOCK_NAME mv_sparse_blocks_wide
#define MV_BLOCK_DOT mv_quant_dot16_sparse_wide
#define MV_BLOCK_SCORES mv_block_scores_wide
#define MV_BLOCK_MASK mv_block_mask_wide
#include "block_scan_impl.h"
#undef MV_BLOCK_TARGET
#undef MV_BLOCK_NAME
#undef MV_BLOCK_DOT
#undef MV_BLOCK_SCORES
#undef MV_BLOCK_MASK
#define MV_BLOCK_TARGET __attribute__((target("avx2")))
#define MV_BLOCK_NAME mv_sparse_bounds_avx2
#define MV_BLOCK_DOT mv_quant_dot16_sparse
#define MV_BLOCK_SCORES mv_block_scores_avx2
#define MV_BLOCK_MASK mv_block_mask_avx2
#include "block_bounds_impl.h"
#undef MV_BLOCK_TARGET
#undef MV_BLOCK_NAME
#undef MV_BLOCK_DOT
#undef MV_BLOCK_SCORES
#undef MV_BLOCK_MASK
#define MV_BLOCK_TARGET __attribute__((target("avx2,avx512f,avx512bw")))
#define MV_BLOCK_NAME mv_sparse_bounds_wide
#define MV_BLOCK_DOT mv_quant_dot16_sparse_wide
#define MV_BLOCK_SCORES mv_block_scores_wide
#define MV_BLOCK_MASK mv_block_mask_wide
#include "block_bounds_impl.h"
#undef MV_BLOCK_TARGET
#undef MV_BLOCK_NAME
#undef MV_BLOCK_DOT
#undef MV_BLOCK_SCORES
#undef MV_BLOCK_MASK
#define MV_BLOCK_TARGET __attribute__((target("avx2")))
#define MV_BLOCK_NAME mv_quant_blocks_avx2
#define MV_BLOCK_DOT mv_quant_dot16
#define MV_BLOCK_SCORES mv_block_scores_avx2
#define MV_BLOCK_MASK mv_block_mask_avx2
#include "block_scan_impl.h"
#undef MV_BLOCK_TARGET
#undef MV_BLOCK_NAME
#undef MV_BLOCK_DOT
#undef MV_BLOCK_SCORES
#undef MV_BLOCK_MASK
#define MV_BLOCK_TARGET __attribute__((target("avx2,avx512f,avx512bw")))
#define MV_BLOCK_NAME mv_quant_blocks_wide
#define MV_BLOCK_DOT mv_quant_dot16_wide
#define MV_BLOCK_SCORES mv_block_scores_wide
#define MV_BLOCK_MASK mv_block_mask_wide
#include "block_scan_impl.h"
#undef MV_BLOCK_TARGET
#undef MV_BLOCK_NAME
#undef MV_BLOCK_DOT
#undef MV_BLOCK_SCORES
#undef MV_BLOCK_MASK
#define MV_BLOCK_TARGET __attribute__((target("avx2")))
#define MV_BLOCK_NAME mv_quant_bounds_avx2
#define MV_BLOCK_DOT mv_quant_dot16
#define MV_BLOCK_SCORES mv_block_scores_avx2
#define MV_BLOCK_MASK mv_block_mask_avx2
#include "block_bounds_impl.h"
#undef MV_BLOCK_TARGET
#undef MV_BLOCK_NAME
#undef MV_BLOCK_DOT
#undef MV_BLOCK_SCORES
#undef MV_BLOCK_MASK
#define MV_BLOCK_TARGET __attribute__((target("avx2,avx512f,avx512bw")))
#define MV_BLOCK_NAME mv_quant_bounds_wide
#define MV_BLOCK_DOT mv_quant_dot16_wide
#define MV_BLOCK_SCORES mv_block_scores_wide
#define MV_BLOCK_MASK mv_block_mask_wide
#include "block_bounds_impl.h"
#undef MV_BLOCK_TARGET
#undef MV_BLOCK_NAME
#undef MV_BLOCK_DOT
#undef MV_BLOCK_SCORES
#undef MV_BLOCK_MASK
static uint64_t tile_bound_calls=0;
uint64_t mv_tile_bound_calls(void){return tile_bound_calls;}
size_t mv_quant_topk_bound_blocks(const unsigned char *tiles,size_t slots,const double *bounds,const uint32_t *ranges,size_t range_count,size_t k,uint32_t *selected){
 ++tile_bound_calls;if(mv_prepare_sparse_query())return mv_block_wide_available()?mv_sparse_bounds_wide(tiles,slots,bounds,ranges,range_count,k,selected):mv_sparse_bounds_avx2(tiles,slots,bounds,ranges,range_count,k,selected);return mv_block_wide_available()?mv_quant_bounds_wide(tiles,slots,bounds,ranges,range_count,k,selected):mv_quant_bounds_avx2(tiles,slots,bounds,ranges,range_count,k,selected);
}
size_t mv_quant_topk_blocks(const unsigned char *tiles,size_t slots,const uint32_t *ranges,size_t range_count,size_t k,uint32_t *selected){
 if(mv_prepare_sparse_query())return mv_block_wide_available()?mv_sparse_blocks_wide(tiles,slots,ranges,range_count,k,selected):mv_sparse_blocks_avx2(tiles,slots,ranges,range_count,k,selected);
 return mv_block_wide_available()?mv_quant_blocks_wide(tiles,slots,ranges,range_count,k,selected):mv_quant_blocks_avx2(tiles,slots,ranges,range_count,k,selected);
}

typedef double (*distance_fn)(const unsigned char *, const double *, size_t, double);
/* Queryable-log prototype: one precision decision across physical extents.
   The single-client caller brackets its scan and retains the query array. */
static const double *prepared_query=NULL;
static size_t prepared_dimension=0;
static distance_fn prepared_distance=NULL;
static int prepared_float=0;
void mv_screen_end_query(void){prepared_query=NULL;prepared_dimension=0;prepared_distance=NULL;prepared_float=0;quant_delta=0;}
static distance_fn choose_screen_distance(size_t dim, size_t active, const double *query, const unsigned char *data,size_t bytes) {
    float_screen_selected=0;
    if(prepared_query==query&&prepared_dimension==dim&&prepared_distance&&mv_screen_simd()){
        if(prepared_float&&!float_screen_enabled)return avx2_distance;
        float_screen_selected=prepared_float;return prepared_distance;
    }
    if(float_screen_enabled && dim>=32 && dim<=256 && active>=16 && mv_screen_simd() && (_mm_getcsr() & 0x8040)==0){
        // Cache only a precision-planning decision, never neighbors or scores.
        // Repeated queries with broad numerical boundaries should not pay for
        // the unsuccessful float pass again on every block and execution.
        if(broad_dimension==dim){
            size_t j=0;double delta2=0;
            for(;j<dim;j++){
                double delta=query[j]-broad_query[j];delta2+=delta*delta;
                if(delta2>broad_radius*broad_radius)break;
            }
            if(j==dim)return avx2_distance;
        }
        size_t stride=8+dim*4,count=bytes/stride;
        size_t samples[3]={0,count/2,count-1};double scale=0;
        for(size_t sample=0;sample<3;sample++)for(size_t d=0;d<dim;d++){
            float value;memcpy(&value,data+samples[sample]*stride+8+d*4,4);
            double delta=fabs((double)value-query[d]);if(delta>scale)scale=delta;
        }
        if(scale<0x1p-60 || scale>0x1p60)return avx2_distance;
        size_t i=0;for(;i<dim;i++){
            float_query[i]=(float)query[i];
            if(!isfinite(float_query[i])||(double)float_query[i]!=query[i])break;
        }
        if(i==dim){float_screen_selected=1;return dim==128?(__builtin_cpu_supports("fma")?avx2_fma_distance128:avx2_float_distance128):avx2_float_distance;}
    }
    /* Masked rows are skipped before calling the distance kernel. Density
       therefore does not reduce the work available within an active vector. */
    return dim >= 32 && active >= 16 && mv_screen_simd()
        ? avx2_distance : scalar_distance;
}

static uint64_t refine_four_calls=0,refine_four_points=0;
uint64_t mv_refine_four_calls(void){return refine_four_calls;}
uint64_t mv_refine_four_points(void){return refine_four_points;}
__attribute__((target("avx2,fma"),noinline))
static void mv_refine_four_avx2(const unsigned char *const *rows,const double *query,double *scores){
 __m256d a=_mm256_setzero_pd(),b=a,c=a,d=a;
 for(size_t j=0;j<128;j+=4){
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
static void mv_refine_four_wide(const unsigned char *const *rows,const double *query,double *scores){
 __m512d a=_mm512_setzero_pd(),b=a,c=a,d=a;
 for(size_t j=0;j<128;j+=8){
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
struct mv_refine_item {double score;uint32_t slot;};
static int mv_compare_refine(const void *a,const void *b){
 const struct mv_refine_item *x=a,*y=b;
 if(x->score<y->score)return -1;if(x->score>y->score)return 1;
 return x->slot<y->slot?-1:x->slot>y->slot;
}
/* Certify ranks using outward intervals around the binary64 SIMD sum.
   The four-row FMA estimator additionally changes product rounding. With 128
   nonnegative squared terms, its accumulation plus the reference product
   rounding is bounded well inside the existing 64*DBL_EPSILON*129 envelope.
   Authoritative distances still use separately rounded products/PositiveSum.
   Overlapping intervals always retain the exact-reduction fallback. */
size_t mv_refine_block_candidates(const unsigned char *view,const uint64_t *records,const double *query,size_t k,uint32_t *positions,size_t count,double *scores,int *certified){
 if(!view||!records||!query||!positions||!scores||!certified||!k||k>1024||!count||count>4096)return SIZE_MAX;
 *certified=0;struct mv_refine_item items[count];size_t first=0;
 static int four_enabled=-1;if(four_enabled<0){const char *flag=getenv("MINIDB_REFINE_FOUR");four_enabled=!flag||strcmp(flag,"0");}
 if(four_enabled&&mv_screen_simd()&&__builtin_cpu_supports("fma")){
  int wide=mv_block_wide_available();
  for(;first+4<=count;first+=4){
   const unsigned char *rows[4];double sums[4];for(size_t j=0;j<4;j++)rows[j]=view+records[positions[first+j]]+8;
   if(wide)mv_refine_four_wide(rows,query,sums);else mv_refine_four_avx2(rows,query,sums);
   for(size_t j=0;j<4;j++){if(!isfinite(sums[j]))return SIZE_MAX;items[first+j]=(struct mv_refine_item){sums[j],positions[first+j]};}
   ++refine_four_calls;refine_four_points+=4;
  }
 }
 for(size_t i=first;i<count;i++){
  const unsigned char *row=view+records[positions[i]]+8;
  double score=mv_screen_simd()?avx2_distance(row,query,128,INFINITY):scalar_distance(row,query,128,INFINITY);
  if(!isfinite(score))return SIZE_MAX;items[i]=(struct mv_refine_item){score,positions[i]};
 }
 qsort(items,count,sizeof(*items),mv_compare_refine);
 double error=64.0*DBL_EPSILON*129;size_t take=count<k?count:k;
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

size_t mv_refine_packed_block_candidates(const unsigned char *view,const uint64_t *records,const double *query,size_t k,uint32_t *positions,size_t count,double *scores,int *certified){
 if(!view||!records||!query||!positions||!scores||!certified||!k||k>1024||!count||count>4096)return SIZE_MAX;
 *certified=0;struct mv_refine_item items[count];size_t first=0;
 static int four_enabled=-1;if(four_enabled<0){const char *flag=getenv("MINIDB_REFINE_FOUR");four_enabled=!flag||strcmp(flag,"0");}
 if(four_enabled&&mv_screen_simd()&&__builtin_cpu_supports("fma")){
  int wide=mv_block_wide_available();
  for(;first+4<=count;first+=4){
   const unsigned char *rows[4];unsigned char decoded[4][512];double sums[4];for(size_t j=0;j<4;j++){uint64_t record=records[positions[first+j]];rows[j]=view+(record&MV_CODEC_OFFSET)+8;if(record&MV_CODEC_PACKED){if(!mv_codec_decode(rows[j],128,decoded[j]))return SIZE_MAX;rows[j]=decoded[j];}}
   if(wide)mv_refine_four_wide(rows,query,sums);else mv_refine_four_avx2(rows,query,sums);
   for(size_t j=0;j<4;j++){if(!isfinite(sums[j]))return SIZE_MAX;items[first+j]=(struct mv_refine_item){sums[j],positions[first+j]};}
   ++refine_four_calls;refine_four_points+=4;
  }
 }
 for(size_t i=first;i<count;i++){
  uint64_t record=records[positions[i]];const unsigned char *row=view+(record&MV_CODEC_OFFSET)+8;unsigned char decoded[512];if(record&MV_CODEC_PACKED){if(!mv_codec_decode(row,128,decoded))return SIZE_MAX;row=decoded;}
  double score=mv_screen_simd()?avx2_distance(row,query,128,INFINITY):scalar_distance(row,query,128,INFINITY);
  if(!isfinite(score))return SIZE_MAX;items[i]=(struct mv_refine_item){score,positions[i]};
 }
 qsort(items,count,sizeof(*items),mv_compare_refine);
 double error=64.0*DBL_EPSILON*129;size_t take=count<k?count:k;
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

int mv_screen_begin_query(const unsigned char *data,size_t bytes,size_t dim,const double *query,size_t active){
    mv_screen_end_query();if(!data||!query||!dim||dim>4096||bytes<(8+dim*4)||bytes%(8+dim*4))return 0;
    distance_fn selected=choose_screen_distance(dim,active,query,data,bytes);
    prepared_query=query;prepared_dimension=dim;prepared_distance=selected;prepared_float=float_screen_selected;return 1;
}

static distance_fn choose_distance(size_t dim,size_t active){
    return dim>=32&&active>=16&&mv_screen_simd()?avx2_distance:scalar_distance;
}

size_t mv_screen(const unsigned char *data, size_t bytes, size_t dim,
                 const double *query, double bound, const unsigned char *mask,
                 uint32_t *selected) {
    if (!dim || dim > 4096 || !data || !query || !selected || isnan(bound) || bound < 0)
        return SIZE_MAX;
    size_t stride = 8 + dim * 4;
    if (bytes % stride || bytes / stride > UINT32_MAX) return SIZE_MAX;
    size_t count = bytes / stride, found = 0;
    size_t active = count;
    if (mask) {
        active = 0;
        for (size_t i = 0; i < count; ++i) active += mask[i] != 0;
    }
    distance_fn distance = choose_screen_distance(dim, active, query, data, bytes);
    /* The float kernel has ceil(dim/16) terms per lane, one pairwise
       lane add, and a binary64 final reduction. (ceil(dim/16)+8)*FLT_EPSILON
       covers subtraction, squaring and accumulation relative error; the
       additive band covers gradual underflow. FTZ/DAZ disable this path.
       Binary64 retains the original wider gamma(dim) band. No FMA/fast-math. */
    double error=(float_screen_selected?((dim+15)/16+8)*FLT_EPSILON:64.0*DBL_EPSILON*(dim+1));
    double slack=float_screen_selected?64.0*FLT_TRUE_MIN*(dim+1):DBL_MIN;
    double cutoff = bound * (1.0 + error) + slack;
    for (size_t i = 0; i < count; ++i) {
        if (mask && !mask[i]) continue;
        const unsigned char *vector = data + i * stride + 8;
        double sum = distance(vector, query, dim, cutoff);
        if (isfinite(sum) && sum <= cutoff) selected[found++] = (uint32_t)i;
    }
    return found;
}

/* Tighten a block's threshold with its approximate local k-th distance.
   Retain the complete conservative boundary band, including all distance ties.
   Python still performs final reference-distance and primary-key ordering. */
size_t mv_screen_topk(const unsigned char *data, size_t bytes, size_t dim,
                      const double *query, double bound, const unsigned char *mask,
                      size_t k, uint32_t *selected) {
    if (!dim || dim > 4096 || !k || k > 1024 || !data || !query || !selected ||
        isnan(bound) || bound < 0 || bytes > (4u << 20)) return SIZE_MAX;
    size_t stride = 8 + dim * 4;
    if (bytes % stride) return SIZE_MAX;
    size_t count = bytes / stride;
    if (!count) return 0;
    size_t active = count;
    if (mask) {
        active = 0;
        for (size_t i = 0; i < count; ++i) active += mask[i] != 0;
    }
    /* Sparse or large-k blocks cannot amortize a local heap. */
    if (active <= 4 * k) return mv_screen(data, bytes, dim, query, bound, mask, selected);
    double distances[count], heap[k];
    distance_fn distance = choose_screen_distance(dim, active, query, data, bytes);
    size_t used = 0, candidates = 0;
    double error = (float_screen_selected?((dim+15)/16+8)*FLT_EPSILON:64.0*DBL_EPSILON*(dim+1));
    double slack=float_screen_selected?64.0*FLT_TRUE_MIN*(dim+1):DBL_MIN;
    double cutoff = bound * (1.0 + error) + slack;
    double guard = (1.0 + error) / (1.0 - error);
    for (size_t i = 0; i < count; ++i) {
        if (mask && !mask[i]) continue;
        const unsigned char *vector = data + i * stride + 8;
        double sum = distance(vector, query, dim, cutoff);
        if (!isfinite(sum)) continue;
        distances[candidates] = sum;
        selected[candidates++] = (uint32_t)i;
        if (used < k) {
            size_t at = used++;
            while (at && heap[(at - 1) / 2] < sum) {
                heap[at] = heap[(at - 1) / 2]; at = (at - 1) / 2;
            }
            heap[at] = sum;
        } else if (sum < heap[0]) {
            size_t at = 0;
            while (at * 2 + 1 < used) {
                size_t child = at * 2 + 1;
                if (child + 1 < used && heap[child + 1] > heap[child]) ++child;
                if (heap[child] <= sum) break;
                heap[at] = heap[child]; at = child;
            }
            heap[at] = sum;
        }
        if (used == k) {
            double local = heap[0] * guard + 2*slack;
            if (local < cutoff) cutoff = local;
        }
    }
    size_t found = 0;
    for (size_t i = 0; i < candidates; ++i)
        if (distances[i] <= cutoff) selected[found++] = selected[i];
    // A tight cluster may make the conservative float boundary too broad.
    // Re-screen in binary64 before expensive exact accumulation explodes.
    if(float_screen_selected && found>4*k && found>active/256){
        broad_dimension=dim;memcpy(broad_query,query,dim*sizeof(double));
        broad_radius=sqrt(heap[0])*sqrt(error)*0.01;
        int enabled=float_screen_enabled;float_screen_enabled=0;
        size_t result=mv_screen_topk(data,bytes,dim,query,bound,mask,k,selected);
        float_screen_enabled=enabled;
        if(prepared_query==query&&prepared_dimension==dim){prepared_distance=avx2_distance;prepared_float=0;}
        return result;
    }
    return found;
}

/* A logical scan block can cross log extents without copying vector records.
   Positions are relative to the concatenation, and retain input order. */
struct mv_record_span {const unsigned char *data,*mask;size_t count;const unsigned char *quant;const uint32_t *indices;size_t index_count,index_base;};
size_t mv_screen_topk_spans(const struct mv_record_span *spans,size_t span_count,size_t dim,const double *query,double bound,size_t k,uint32_t *selected);
#define MV_SPAN_TARGET __attribute__((target("avx2")))
#define MV_SPAN_NAME mv_spans_quant
#define MV_SPAN_DISTANCE quant_distance128
#define MV_SPAN_QUANT
#include "span_scan.inc"
#undef MV_SPAN_TARGET
#undef MV_SPAN_NAME
#undef MV_SPAN_DISTANCE
#undef MV_SPAN_QUANT
#define MV_SPAN_TARGET __attribute__((target("avx2,fma")))
#define MV_SPAN_NAME mv_spans_fma
#define MV_SPAN_DISTANCE avx2_fma_distance128
#include "span_scan.inc"
#undef MV_SPAN_TARGET
#undef MV_SPAN_NAME
#undef MV_SPAN_DISTANCE
#define MV_SPAN_TARGET
#define MV_SPAN_NAME mv_spans_generic
#define MV_SPAN_DISTANCE distance
#define MV_SPAN_DISPATCH
#include "span_scan.inc"
#undef MV_SPAN_TARGET
#undef MV_SPAN_NAME
#undef MV_SPAN_DISTANCE
#undef MV_SPAN_DISPATCH
size_t mv_screen_topk_spans(const struct mv_record_span *spans,size_t span_count,size_t dim,const double *query,double bound,size_t k,uint32_t *selected){return mv_spans_generic(spans,span_count,dim,query,bound,k,selected);}

/* Build the predicate byte mask for one index block.  Both id arrays are
   sorted ascending; allowed == NULL means every key is allowed. */
size_t mv_id_mask(const unsigned char *data, size_t bytes, size_t dim,
                  const int64_t *allowed, size_t allowed_count,
                  const int64_t *excluded, size_t excluded_count,
                  unsigned char *mask) {
    if (!data || !mask || !dim || dim > 4096) return SIZE_MAX;
    size_t stride = 8 + dim * 4;
    if (bytes % stride) return SIZE_MAX;
    size_t count = bytes / stride;
    for (size_t i = 0; i < count; ++i) {
        int64_t ident; memcpy(&ident, data + i * stride, 8);
        int keep = 1;
        if (allowed) {
            size_t lo = 0, hi = allowed_count;
            keep = 0;
            while (lo < hi) {
                size_t mid = lo + (hi - lo) / 2;
                if (allowed[mid] < ident) lo = mid + 1;
                else if (allowed[mid] > ident) hi = mid;
                else { keep = 1; break; }
            }
        }
        if (keep && excluded_count) {
            size_t lo = 0, hi = excluded_count;
            while (lo < hi) {
                size_t mid = lo + (hi - lo) / 2;
                if (excluded[mid] < ident) lo = mid + 1;
                else if (excluded[mid] > ident) hi = mid;
                else { keep = 0; break; }
            }
        }
        mask[i] = (unsigned char)keep;
    }
    return count;
}

#include "graph.c"

#include "approx.c"

#include "vector_codec_impl.h"
