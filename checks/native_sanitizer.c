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
    double terms[128];
    assert(mv_squared_terms(rows,sizeof(bytes)-1,128,query,0,terms));
    assert(terms[0]==1 && terms[127]==0);
    assert(!mv_squared_terms(rows,sizeof(bytes)-1,128,query,3,terms));
    assert(!mv_squared_terms(rows,sizeof(bytes)-2,128,query,0,terms));
    assert(!mv_squared_terms(rows,sizeof(bytes)-1,4097,query,0,terms));
    assert(!mv_squared_terms(NULL,0,128,query,0,terms));
    query[0]=INFINITY;
    assert(!mv_squared_terms(rows,sizeof(bytes)-1,128,query,0,terms));
    query[0]=0;
    size_t n = mv_screen(rows, sizeof(bytes)-1, 128, query, 0, 0, selected);
    assert(n == 1 && selected[0] == 1);
    assert(mv_screen(rows, sizeof(bytes)-1, 128, query, INFINITY, mask, selected) == 1);
    assert(mv_screen(rows, sizeof(bytes)-2, 128, query, 0, 0, selected) == SIZE_MAX);
    assert(mv_screen(rows, sizeof(bytes)-1, 4097, query, 0, 0, selected) == SIZE_MAX);
    assert(mv_screen(rows, sizeof(bytes)-1, 0, query, 0, 0, selected) == SIZE_MAX);
    assert(mv_screen(rows, sizeof(bytes)-1, 128, query, -1, 0, selected) == SIZE_MAX);
    n=mv_screen_topk(rows,sizeof(bytes)-1,128,query,INFINITY,0,1,selected);
    assert(n==3 && selected[1]==1);
    assert(mv_screen_topk(rows,sizeof(bytes)-1,128,query,INFINITY,mask,1,selected)==1);
    assert(mv_screen_topk(rows,sizeof(bytes)-1,128,query,INFINITY,0,0,selected)==SIZE_MAX);
    assert(mv_screen_topk(rows,sizeof(bytes)-1,128,query,INFINITY,0,1025,selected)==SIZE_MAX);
    assert(mv_screen_topk(rows,sizeof(bytes)-2,128,query,INFINITY,0,1,selected)==SIZE_MAX);
    unsigned char dense[10 * (8 + 128 * 4)] = {0};
    uint32_t output[10];
    for (size_t i=0;i<10;++i) {
        float v=(float)i; memcpy(dense+i*(8+128*4)+8,&v,4);
    }
    assert(mv_screen_topk(dense,sizeof(dense),128,query,INFINITY,0,1,output)==1 && output[0]==0);
    memset(dense,0,sizeof(dense));
    assert(mv_screen_topk(dense,sizeof(dense),128,query,INFINITY,0,1,output)==10);
    mv_screen_set_simd(0);
    assert(mv_screen_topk(dense,sizeof(dense),128,query,INFINITY,0,1,output)==10);
    mv_screen_set_simd(1);
    assert(mv_screen_topk(dense,sizeof(dense),128,query,INFINITY,0,1,output)==10);
    return 0;
}
