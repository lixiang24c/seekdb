#include <assert.h>
#include "../native/screen.c"
int main(void) {
    unsigned char bytes[1 + 3 * (8 + 128 * 4)] = {0};
    unsigned char *rows = bytes + 1; /* Intentionally unaligned. */
    double query[128] = {0};
    uint32_t selected[3] = {0};
    unsigned char mask[3] = {0, 1, 0};
    float one = 1.f;
    memcpy(rows + 8, &one, 4);
    memcpy(rows + 2 * (8 + 128 * 4) + 8, &one, 4);
    size_t n = mv_screen(rows, sizeof(bytes)-1, 128, query, 0, 0, selected);
    assert(n == 1 && selected[0] == 1);
    assert(mv_screen(rows, sizeof(bytes)-1, 128, query, INFINITY, mask, selected) == 1);
    assert(mv_screen(rows, sizeof(bytes)-2, 128, query, 0, 0, selected) == SIZE_MAX);
    assert(mv_screen(rows, sizeof(bytes)-1, 4097, query, 0, 0, selected) == SIZE_MAX);
    assert(mv_screen(rows, sizeof(bytes)-1, 0, query, 0, 0, selected) == SIZE_MAX);
    assert(mv_screen(rows, sizeof(bytes)-1, 128, query, -1, 0, selected) == SIZE_MAX);
    return 0;
}
