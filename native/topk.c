/* Self-contained exact L2/TopK math kernel. No storage/index library. */
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <immintrin.h>

typedef struct { double distance; int64_t key; uint64_t offset; } Hit;
static int less(Hit a, Hit b) {
    return a.distance < b.distance || (a.distance == b.distance && a.key < b.key);
}
static double scalar_l2(const float *v, const double *q, size_t dim) {
    double sum = 0;
    for (size_t j=0;j<dim;j++) { double d=(double)v[j]-q[j];sum+=d*d; }
    return sum;
}
__attribute__((target("avx2")))
static double avx2_l2(const float *v, const double *q, size_t dim) {
    __m256d acc=_mm256_setzero_pd();size_t j=0;
    for (;j+4<=dim;j+=4) {
        __m256d d=_mm256_sub_pd(_mm256_cvtps_pd(_mm_loadu_ps(v+j)),_mm256_loadu_pd(q+j));
        acc=_mm256_add_pd(acc,_mm256_mul_pd(d,d));
    }
    double lanes[4];_mm256_storeu_pd(lanes,acc);
    double sum=(lanes[0]+lanes[1])+(lanes[2]+lanes[3]);
    for (;j<dim;j++) { double d=(double)v[j]-q[j];sum+=d*d; }
    return sum;
}
/* Return SIZE_MAX on allocation failure; otherwise output ascending (distance, key). */
size_t mv_topk(const unsigned char *rows,size_t count,size_t stride,size_t dim,
               const double *query,const unsigned char *mask,size_t k,int use_simd,
               double *distances,int64_t *keys,uint64_t *offsets) {
    if (!k || !count) return 0;
    if (k>count) k=count;
    Hit *heap=(Hit*)malloc(k*sizeof(Hit));if (!heap) return SIZE_MAX;
    size_t used=0;
    int avx=use_simd && __builtin_cpu_supports("avx2");
    for (size_t i=0;i<count;i++) {
        if (mask && !mask[i]) continue;
        const unsigned char *row=rows+i*stride;Hit hit;
        memcpy(&hit.key,row,8);memcpy(&hit.offset,row+8,8);
        const float *vector=(const float*)(row+16);
        hit.distance=avx ? avx2_l2(vector,query,dim) : scalar_l2(vector,query,dim);
        size_t at;
        if (used<k) {
            at=used++;heap[at]=hit;
            while (at) {
                size_t parent=(at-1)/2;
                if (!less(heap[parent],heap[at])) break;
                Hit temp=heap[parent];heap[parent]=heap[at];heap[at]=temp;at=parent;
            }
        } else {
            if (!less(hit,heap[0])) continue;
            heap[0]=hit;at=0;
            for (;;) {
                size_t child=at*2+1;if (child>=used) break;
                if (child+1<used && less(heap[child],heap[child+1])) child++;
                if (!less(heap[at],heap[child])) break;
                Hit temp=heap[at];heap[at]=heap[child];heap[child]=temp;at=child;
            }
        }
    }
    size_t total=used;
    while (used) {
        Hit hit=heap[0];size_t output=used-1;
        distances[output]=hit.distance;keys[output]=hit.key;offsets[output]=hit.offset;
        heap[0]=heap[--used];size_t at=0;
        for (;;) {
            size_t child=at*2+1;if (child>=used) break;
            if (child+1<used && less(heap[child],heap[child+1])) child++;
            if (!less(heap[at],heap[child])) break;
            Hit temp=heap[at];heap[at]=heap[child];heap[child]=temp;at=child;
        }
    }
    free(heap);return total;
}
