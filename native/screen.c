/* Self-built screening kernel; final distance/ranking stays in Python. */
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <float.h>
#include <math.h>
#include <immintrin.h>

unsigned mv_screen_abi(void) { return 4; }

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

typedef double (*distance_fn)(const unsigned char *, const double *, size_t, double);
static distance_fn choose_distance(size_t dim, size_t count, size_t active) {
    /* Sparse/tiny blocks spend most time in masks and call overhead. */
    return dim >= 32 && active >= 16 && active >= (count + 1) / 2 && mv_screen_simd()
        ? avx2_distance : scalar_distance;
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
    distance_fn distance = choose_distance(dim, count, active);
    /* Positive-term binary64 summation error is bounded by gamma(dim).
       This deliberately wider band also covers cutoff multiplication rounding.
       No fast-math or FMA contraction; stored/query values are finite float32. */
    double cutoff = bound * (1.0 + 64.0 * DBL_EPSILON * (dim + 1)) + DBL_MIN;
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
        isnan(bound) || bound < 0 || bytes > 65536) return SIZE_MAX;
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
    distance_fn distance = choose_distance(dim, count, active);
    size_t used = 0;
    double error = 64.0 * DBL_EPSILON * (dim + 1);
    double cutoff = bound * (1.0 + error) + DBL_MIN;
    double guard = (1.0 + error) / (1.0 - error);
    for (size_t i = 0; i < count; ++i) {
        distances[i] = INFINITY;
        if (mask && !mask[i]) continue;
        const unsigned char *vector = data + i * stride + 8;
        double sum = distance(vector, query, dim, cutoff);
        if (!isfinite(sum)) continue;
        distances[i] = sum;
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
            double local = heap[0] * guard + DBL_MIN;
            if (local < cutoff) cutoff = local;
        }
    }
    size_t found = 0;
    for (size_t i = 0; i < count; ++i)
        if (isfinite(distances[i]) && distances[i] <= cutoff) selected[found++] = (uint32_t)i;
    return found;
}

#include "graph.c"
