/* Self-built screening kernel; final distance/ranking stays in Python. */
#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <float.h>
#include <math.h>

unsigned mv_screen_abi(void) { return 1; }

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
