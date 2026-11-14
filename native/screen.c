#define _POSIX_C_SOURCE 200809L
/* Screening and reference terms; Python resolves inexact sums and ranking. */
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <float.h>
#include <math.h>
#include <immintrin.h>

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
static double quant_delta=0,quant_query_norm=0,quant_query_scale=0;
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
 quant_delta=nextafter((2*(qnorm*error+(norm+error)*qe)+1e-10*(norm*norm+quant_query_norm))*(1+1e-10),INFINITY);
 double sampled=0;for(size_t j=0;j<128;j++){float x;memcpy(&x,sample+j*4,4);double d=(double)x-q[j];sampled+=d*d;}
 return isfinite(quant_delta)&&quant_delta<=sampled*.03;
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
