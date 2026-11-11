#include <assert.h>
#include "../native/screen.c"
int main(void) {
    assert(!mv_graph_new(2,128,16<<20));
    assert(!mv_graph_new(1024,257,16<<20));
    assert(!mv_graph_new(1024,128,100));
    for(size_t dim=16;dim<=32;dim*=2) {
        MVGraph *g=mv_graph_new(1024,dim,16<<20);assert(g);
        size_t stride=8+dim*4;
        unsigned char *data=calloc(1024,stride);assert(data);
        for(size_t i=0;i<1024;++i) {
            int64_t id=(int64_t)i-512;memcpy(data+i*stride,&id,8);
            for(size_t j=0;j<dim;++j){float v=(float)i/1024.f;memcpy(data+i*stride+8+4*j,&v,4);}
        }
        assert(!mv_graph_append(g,data,1024*stride-1,NULL));
        assert(mv_graph_append(g,data,1024*stride,NULL));
        assert(mv_graph_build(g));
        double query[32];for(size_t j=0;j<dim;++j)query[j]=.1234567;
        int64_t ids[10];size_t found=mv_graph_search(g,query,10,128,ids);
        assert(found==10 && ids[0]==126-512);
        assert(mv_graph_search(g,query,0,128,ids)==SIZE_MAX);
        assert(mv_graph_search(g,query,10,2,ids)==SIZE_MAX);
        for(size_t j=0;j<dim;++j)query[j]=0;
        assert(mv_graph_search(g,query,10,128,ids)==10 && ids[0]==-512);
        mv_graph_free(g);free(data);
    }
    MVGraph *flat=mv_graph_new(1024,16,16<<20);assert(flat);
    unsigned char *zeros=calloc(1024,72);assert(zeros);
    assert(mv_graph_append(flat,zeros,1024*72,NULL));
    assert(!mv_graph_append(flat,zeros,72,NULL));
    assert(!mv_graph_build(flat));
    mv_graph_free(flat);free(zeros);
    mv_graph_free(NULL);return 0;
}
