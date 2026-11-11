/* Self-built screening kernel; final distance/ranking stays in Python. */
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <float.h>
#include <math.h>

unsigned mv_screen_abi(void) { return 2; }

size_t mv_screen(const unsigned char *data, size_t bytes, size_t dim,
                 const double *query, double bound, const unsigned char *mask,
                 uint32_t *selected) {
    if (!dim || dim > 4096 || !data || !query || !selected || isnan(bound) || bound < 0)
        return SIZE_MAX;
    size_t stride = 8 + dim * 4;
    if (bytes % stride || bytes / stride > UINT32_MAX) return SIZE_MAX;
    size_t count = bytes / stride, found = 0;
    /* Positive-term binary64 summation error is bounded by gamma(dim).
       This deliberately wider band also covers cutoff multiplication rounding.
       No fast-math or FMA contraction; stored/query values are finite float32. */
    double cutoff = bound * (1.0 + 64.0 * DBL_EPSILON * (dim + 1)) + DBL_MIN;
    for (size_t i = 0; i < count; ++i) {
        if (mask && !mask[i]) continue;
        const unsigned char *vector = data + i * stride + 8;
        double sum = 0;
        size_t j = 0;
        for (; j < dim; ++j) {
            float value;
            memcpy(&value, vector + j * 4, 4); /* Also handles unaligned records. */
            double delta = (double)value - query[j];
            sum += delta * delta;
            if (sum > cutoff) break;
        }
        if (j == dim) selected[found++] = (uint32_t)i;
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
    size_t used = 0;
    double error = 64.0 * DBL_EPSILON * (dim + 1);
    double cutoff = bound * (1.0 + error) + DBL_MIN;
    double guard = (1.0 + error) / (1.0 - error);
    for (size_t i = 0; i < count; ++i) {
        distances[i] = INFINITY;
        if (mask && !mask[i]) continue;
        const unsigned char *vector = data + i * stride + 8;
        double sum = 0;
        size_t j = 0;
        for (; j < dim; ++j) {
            float value;
            memcpy(&value, vector + j * 4, 4);
            double delta = (double)value - query[j];
            sum += delta * delta;
            if (sum > cutoff) break;
        }
        if (j != dim) continue;
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
