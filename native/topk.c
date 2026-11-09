/* Self-contained exact L2/TopK math kernel. No storage/index library. */
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>
#include <string.h>
#include <immintrin.h>
#include <float.h>
#include <math.h>

/* Uniform byte codes are only a rejection screen. All winners are evaluated
   against original float32 vectors with the normal double-precision kernel. */
int mv_quantize(const unsigned char *rows,size_t count,size_t stride,size_t dim,
                double *minimum,double *step,unsigned char *codes) {
    if (!count || !dim || dim>4096) return 0;
    double lo=DBL_MAX,hi=-DBL_MAX;
    for (size_t i=0;i<count;i++) {
        const float *v=(const float*)(rows+i*stride+16);
        for (size_t j=0;j<dim;j++) {
            if (!isfinite(v[j])) return 0;
            if (v[j]<lo) lo=v[j];
            if (v[j]>hi) hi=v[j];
        }
    }
    double scale=(hi-lo)/255.;
    if (!(scale>0) || !isfinite(scale)) return 0;
    for (size_t i=0;i<count;i++) {
        const float *v=(const float*)(rows+i*stride+16);
        for (size_t j=0;j<dim;j++) {
            double normalized=((double)v[j]-lo)/scale;
            unsigned int code=(unsigned int)(normalized+.5);
            if (code>255) code=255;
            /* Verify the per-coordinate reconstruction bound at build time. */
            if (fabs((double)v[j]-(lo+scale*code))>scale) return 0;
            codes[i*dim+j]=(unsigned char)code;
        }
    }
    *minimum=lo;*step=scale;return 1;
}

__attribute__((target("avx2")))
static uint32_t byte_l2_avx2(const unsigned char *v,const unsigned char *q,size_t dim) {
    __m256i acc=_mm256_setzero_si256();size_t j=0;
    for (;j+16<=dim;j+=16) {
        __m256i a=_mm256_cvtepu8_epi16(_mm_loadu_si128((const __m128i*)(v+j)));
        __m256i b=_mm256_cvtepu8_epi16(_mm_loadu_si128((const __m128i*)(q+j)));
        __m256i delta=_mm256_sub_epi16(a,b);
        acc=_mm256_add_epi32(acc,_mm256_madd_epi16(delta,delta));
    }
    uint32_t lanes[8],sum=0;_mm256_storeu_si256((__m256i*)lanes,acc);
    for (size_t i=0;i<8;i++) sum+=lanes[i];
    for (;j<dim;j++) {int delta=(int)v[j]-(int)q[j];sum+=(uint32_t)(delta*delta);}
    return sum;
}

static uint32_t byte_l2_scalar(const unsigned char *v,const unsigned char *q,size_t dim) {
    uint32_t sum=0;
    for (size_t j=0;j<dim;j++) {int d=(int)v[j]-(int)q[j];sum+=(uint32_t)(d*d);}
    return sum;
}

typedef struct { double distance; int64_t key; uint64_t offset; } Hit;
static int less(Hit a, Hit b) {
    return a.distance < b.distance || (a.distance == b.distance && a.key < b.key);
}
static double scalar_l2(const float *v, const double *q, size_t dim) {
    double sum = 0;
    for (size_t j=0;j<dim;j++) { double d=(double)v[j]-q[j];sum+=d*d; }
    return sum;
}
static double scalar_bounded_l2(const float *v,const double *q,size_t dim,double cutoff) {
    if (dim<32) return scalar_l2(v,q,dim);
    size_t split=dim-dim/4;double sum=0;
    for (size_t j=0;j<split;j++) {double d=(double)v[j]-q[j];sum+=d*d;}
    if (sum>cutoff) return sum;
    for (size_t j=split;j<dim;j++) {double d=(double)v[j]-q[j];sum+=d*d;}
    return sum;
}
__attribute__((target("avx2")))
static double avx2_l2(const float *v,const double *q,size_t dim,double cutoff) {
    __m256d acc=_mm256_setzero_pd();size_t j=0;
    size_t split=dim<32?0:(dim-dim/4)&~(size_t)3;
    for (;j<split;j+=4) {
        __m256d d=_mm256_sub_pd(_mm256_cvtps_pd(_mm_loadu_ps(v+j)),_mm256_loadu_pd(q+j));
        acc=_mm256_add_pd(acc,_mm256_mul_pd(d,d));
    }
    double lanes[4];
    if (split) {
        _mm256_storeu_pd(lanes,acc);
        double partial=(lanes[0]+lanes[1])+(lanes[2]+lanes[3]);
        /* Nonnegative lane accumulation is monotone. Equality must continue:
           a later row with a smaller primary key can still win a distance tie. */
        if (partial>cutoff) return partial;
    }
    for (;j+4<=dim;j+=4) {
        __m256d d=_mm256_sub_pd(_mm256_cvtps_pd(_mm_loadu_ps(v+j)),_mm256_loadu_pd(q+j));
        acc=_mm256_add_pd(acc,_mm256_mul_pd(d,d));
    }
    _mm256_storeu_pd(lanes,acc);
    double sum=(lanes[0]+lanes[1])+(lanes[2]+lanes[3]);
    for (;j<dim;j++) {double d=(double)v[j]-q[j];sum+=d*d;}
    return sum;
}
/* Return SIZE_MAX on allocation failure; otherwise output ascending (distance, key). */
static inline __attribute__((always_inline)) size_t topk(const unsigned char *rows,size_t count,size_t stride,size_t dim,
               const double *query,const unsigned char *mask,const uint32_t *positions,
               size_t candidates,const unsigned char *codes,double minimum,double step,size_t k,int use_simd,
               double *distances,int64_t *keys,uint64_t *offsets) {
    if (!k || !count) return 0;
    if (k>count) k=count;
    Hit *heap=(Hit*)malloc(k*sizeof(Hit));if (!heap) return SIZE_MAX;
    size_t used=0;
    int avx=use_simd && __builtin_cpu_supports("avx2");
    unsigned char *query_codes=NULL;
    if (codes && dim>=32 && dim<=4096 && candidates>=512 && k<=candidates/4 &&
        step>0 && isfinite(step) && isfinite(minimum)) {
        query_codes=malloc(dim);
        if (query_codes) for (size_t j=0;j<dim;j++) {
            double normalized=(query[j]-minimum)/step;
            if (!isfinite(normalized) || normalized<0 || normalized>255) {
                free(query_codes);query_codes=NULL;break;
            }
            unsigned int code=(unsigned int)(normalized+.5);
            if (code>255) code=255;
            if (fabs(query[j]-(minimum+step*code))>step) {
                free(query_codes);query_codes=NULL;break;
            }
            query_codes[j]=(unsigned char)code;
        }
    }
    double last_cutoff=-1,integer_limit=DBL_MAX;
    double residual=2*sqrt((double)dim)+1;
    for (size_t candidate=0;candidate<candidates;candidate++) {
        size_t i=positions?positions[candidate]:candidate;
        if (i>=count) continue;
        if (mask && !mask[i]) continue;
        double cutoff=used==k?heap[0].distance:DBL_MAX;
        if (query_codes && used==k) {
            if (cutoff!=last_cutoff) {
                /* Each reconstruction has L2 error <= step*sqrt(dim).
                   The extra grid unit and upward slack cover FP rounding,
                   including double accumulation in the exact kernel. */
                double radius=sqrt(cutoff)/step+residual;
                integer_limit=radius*radius*(1+1e-9);last_cutoff=cutoff;
            }
            uint32_t squared=avx?byte_l2_avx2(codes+i*dim,query_codes,dim):byte_l2_scalar(codes+i*dim,query_codes,dim);
            if ((double)squared>integer_limit) continue;
        }
        const unsigned char *row=rows+i*stride;Hit hit;
        memcpy(&hit.key,row,8);memcpy(&hit.offset,row+8,8);
        const float *vector=(const float*)(row+16);
        hit.distance=avx ? avx2_l2(vector,query,dim,cutoff) : scalar_bounded_l2(vector,query,dim,cutoff);
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
    free(query_codes);free(heap);return total;
}

size_t mv_topk(const unsigned char *rows,size_t count,size_t stride,size_t dim,
               const double *query,const unsigned char *mask,size_t k,int use_simd,
               double *distances,int64_t *keys,uint64_t *offsets) {
    return topk(rows,count,stride,dim,query,mask,NULL,count,NULL,0,0,k,use_simd,distances,keys,offsets);
}

size_t mv_topk_selected(const unsigned char *rows,size_t count,size_t stride,size_t dim,
               const double *query,const uint32_t *positions,size_t candidates,size_t k,int use_simd,
               double *distances,int64_t *keys,uint64_t *offsets) {
    return topk(rows,count,stride,dim,query,NULL,positions,candidates,NULL,0,0,k,use_simd,distances,keys,offsets);
}

size_t mv_topk_quantized(const unsigned char *rows,size_t count,size_t stride,size_t dim,
               const double *query,const unsigned char *codes,double minimum,double step,
               const void *selection,size_t candidates,int sparse,size_t k,int use_simd,
               double *distances,int64_t *keys,uint64_t *offsets) {
    return topk(rows,count,stride,dim,query,sparse?NULL:selection,sparse?selection:NULL,
                sparse?candidates:count,codes,minimum,step,k,use_simd,distances,keys,offsets);
}

#ifdef MV_WITH_PYTHON
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <math.h>
static PyObject *float32_vector(PyObject *self,PyObject *args) {
    (void)self;PyObject *input;Py_ssize_t dimension;
    if (!PyArg_ParseTuple(args,"On:float32_vector",&input,&dimension)) return NULL;
    int list=PyList_CheckExact(input),tuple=PyTuple_CheckExact(input);
    if ((!list&&!tuple) || dimension<0 || (list?PyList_GET_SIZE(input):PyTuple_GET_SIZE(input))!=dimension) {
        PyErr_SetString(PyExc_ValueError,"vector dimension or sequence mismatch");return NULL;
    }
    PyObject *output=PyList_New(dimension);if (!output) return NULL;
    for (Py_ssize_t i=0;i<dimension;i++) {
        PyObject *item=list?PyList_GET_ITEM(input,i):PyTuple_GET_ITEM(input,i);double number;
        if (PyFloat_CheckExact(item)) number=PyFloat_AS_DOUBLE(item);
        else if (PyLong_CheckExact(item)) {
            number=PyLong_AsDouble(item);
            if (PyErr_Occurred()) {Py_DECREF(output);return NULL;}
        } else {
            Py_DECREF(output);PyErr_SetString(PyExc_ValueError,"invalid float32 type");return NULL;
        }
        float stored=(float)number;
        if (!isfinite(number)||!isfinite(stored)) {
            Py_DECREF(output);PyErr_SetString(PyExc_ValueError,"invalid finite float32");return NULL;
        }
        PyObject *value=PyFloat_FromDouble((double)stored);
        if (!value) {Py_DECREF(output);return NULL;}
        PyList_SET_ITEM(output,i,value);
    }
    return output;
}
static PyMethodDef methods[]={
    {"float32_vector",float32_vector,METH_VARARGS,"Validate and round finite float32 values."},
    {NULL,NULL,0,NULL}
};
static struct PyModuleDef module={PyModuleDef_HEAD_INIT,"libminivector",NULL,-1,methods};
PyMODINIT_FUNC PyInit_libminivector(void) {return PyModule_Create(&module);}
#endif
