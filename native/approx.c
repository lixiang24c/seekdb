/* Immutable 8-bit VA cache. Actual float32 bin extrema, not estimated error. */
#include <stdlib.h>
#include <unistd.h>
#include <sys/stat.h>

typedef struct {
    size_t count, dim, cost;
    unsigned char *codes;
    float *low, *high;
} mv_va;

void mv_va_free(void *ptr) {
    mv_va *v = ptr;
    if (!v) return;
    free(v->codes); free(v->low); free(v->high); free(v);
}

void *mv_va_build(int fd, size_t dim, size_t budget) {
    struct stat st;
    if (dim < 16 || dim > 256 || fstat(fd, &st) || st.st_size < 8) return NULL;
    size_t stride = 8 + dim * 4, bytes = (size_t)st.st_size - 8;
    if (bytes % stride) return NULL;
    size_t count = bytes / stride;
    if (count < 512 || count > 20000) return NULL;
    size_t cost = sizeof(mv_va) + count * dim + 2 * dim * 256 * sizeof(float);
    if (cost > budget) return NULL;
    unsigned char header[8];
    if (pread(fd, header, 8, 0) != 8 || memcmp(header, "MVFLT001", 8)) return NULL;
    mv_va *v = calloc(1, sizeof(*v));
    if (!v) return NULL;
    v->count = count; v->dim = dim; v->cost = cost;
    v->codes = malloc(count * dim);
    v->low = malloc(dim * 256 * sizeof(float));
    v->high = malloc(dim * 256 * sizeof(float));
    if (!v->codes || !v->low || !v->high) { mv_va_free(v); return NULL; }
    double minimum[256], maximum[256];
    for (size_t d = 0; d < dim; ++d) { minimum[d] = INFINITY; maximum[d] = -INFINITY; }
    for (size_t j = 0; j < dim * 256; ++j) { v->low[j] = INFINITY; v->high[j] = -INFINITY; }
    unsigned char data[65536];
    size_t block = sizeof(data) / stride;
    for (int pass = 0; pass < 2; ++pass) {
        for (size_t start = 0; start < count; start += block) {
            size_t n = count - start; if (n > block) n = block;
            size_t size = n * stride, done = 0;
            while (done < size) {
                ssize_t got = pread(fd, data + done, size - done, 8 + start * stride + done);
                if (got <= 0) { mv_va_free(v); return NULL; }
                done += (size_t)got;
            }
            for (size_t i = 0; i < n; ++i) for (size_t d = 0; d < dim; ++d) {
                float x; memcpy(&x, data + i * stride + 8 + d * 4, 4);
                if (!isfinite(x)) { mv_va_free(v); return NULL; }
                if (!pass) {
                    if (x < minimum[d]) minimum[d] = x;
                    if (x > maximum[d]) maximum[d] = x;
                } else {
                    double span = maximum[d] - minimum[d];
                    unsigned b = span ? (unsigned)(((double)x - minimum[d]) / span * 256.0) : 0;
                    if (b > 255) b = 255;
                    v->codes[(start + i) * dim + d] = (unsigned char)b;
                    size_t j = d * 256 + b;
                    if (x < v->low[j]) v->low[j] = x;
                    if (x > v->high[j]) v->high[j] = x;
                }
            }
        }
    }
    return v;
}

/* Return SIZE_MAX for sequential fallback; selected positions stay in file order.
   Finite float32 differences and squared terms are normal finite binary64 (unless
   zero). Monotone subtraction/product rounding preserves per-term interval order.
   The inflated gamma(dim) band encloses accumulation and reference fsum rounding.
   Equality is retained, so the caller can resolve primary-key ties exactly. */
size_t mv_va_select(void *ptr, int fd, const double *query, const unsigned char *mask,
                    size_t mask_start, size_t mask_size, size_t k,
                    uint32_t *selected, size_t capacity) {
    mv_va *v = ptr;
    if (!v || !query || !selected || !k || k > 1024) return SIZE_MAX;
    if (mask && (mask_start > v->count || mask_size > v->count - mask_start)) return SIZE_MAX;
    double *tables = malloc(v->dim * 256 * sizeof(double));
    double *bounds = malloc(v->count * sizeof(double));
    if (!tables || !bounds) { free(tables); free(bounds); return SIZE_MAX; }
    for (size_t d = 0; d < v->dim; ++d) {
        if (!isfinite(query[d]) || fabs(query[d]) > FLT_MAX || (double)(float)query[d] != query[d]) {
            free(tables); free(bounds); return SIZE_MAX;
        }
        for (size_t b = 0; b < 256; ++b) {
            size_t j = d * 256 + b;
            double lo = v->low[j], hi = v->high[j], q = query[d];
            double near = q < lo ? lo - q : q > hi ? q - hi : 0;
            tables[j] = near * near;
        }
    }
    double heap[1024]; uint32_t seeds[1024]; size_t used = 0;
    double error = 64.0 * DBL_EPSILON * (v->dim + 1);
    for (size_t i = 0; i < v->count; ++i) {
        bounds[i] = INFINITY;
        if (mask && (i < mask_start || i - mask_start >= mask_size || !mask[i - mask_start])) continue;
        double lower_sum = 0;
        for (size_t d = 0; d < v->dim; ++d) {
            size_t j = d * 256 + v->codes[i * v->dim + d];
            lower_sum += tables[j];
        }
        bounds[i] = lower_sum * (1.0 - error) - DBL_MIN;
        double score = bounds[i];
        if (used < k) {
            size_t at = used++;
            while (at && heap[(at - 1) / 2] < score) { heap[at] = heap[(at - 1) / 2]; seeds[at] = seeds[(at - 1) / 2]; at = (at - 1) / 2; }
            heap[at] = score; seeds[at] = (uint32_t)i;
        } else if (score < heap[0]) {
            size_t at = 0;
            while (at * 2 + 1 < used) {
                size_t child = at * 2 + 1;
                if (child + 1 < used && heap[child + 1] > heap[child]) ++child;
                if (heap[child] <= score) break;
                heap[at] = heap[child]; seeds[at] = seeds[child]; at = child;
            }
            heap[at] = score; seeds[at] = (uint32_t)i;
        }
    }
    /* Any k visible original vectors establish a safe upper bound. Choosing
       the k smallest lower bounds usually makes this bound tight. */
    double threshold = used < k ? INFINITY : 0;
    if (used == k) {
        unsigned char raw[8 + 256 * 4];
        for (size_t i = 0; i < used; ++i) {
            size_t stride = 8 + v->dim * 4, done = 0;
            while (done < stride) {
                ssize_t got = pread(fd, raw + done, stride - done, 8 + seeds[i] * stride + done);
                if (got <= 0) { free(tables); free(bounds); return SIZE_MAX; }
                done += (size_t)got;
            }
            double score = scalar_distance(raw + 8, query, v->dim, INFINITY);
            score = score * (1.0 + error) + DBL_MIN;
            if (score > threshold) threshold = score;
        }
    }
    size_t found = 0, pages = 0, last_page = SIZE_MAX, stride = 8 + v->dim * 4;
    for (size_t i = 0; i < v->count; ++i) {
        if (!isfinite(bounds[i]) || bounds[i] > threshold) continue;
        if (found == capacity) { found = SIZE_MAX; break; }
        selected[found++] = (uint32_t)i;
        size_t first = (8 + i * stride) / 4096, last = (8 + (i + 1) * stride - 1) / 4096;
        if (last_page == SIZE_MAX || first > last_page) pages += last - first + 1;
        else if (last > last_page) pages += last - last_page;
        last_page = last;
    }
    if (pages * 4 > (8 + v->count * stride + 4095) / 4096) found = SIZE_MAX;
    free(tables); free(bounds);
    return found;
}
