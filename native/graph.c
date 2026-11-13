/* Bounded in-memory HNSW-style experimental accelerator, no external index. */
#include <stdlib.h>
#define GM0 24u
#define GM 12u
#define GEF 160u
#define GMAXEF 4096u
#define GMAXLEVEL 12u

typedef struct { double d; uint32_t id; } GHit;
typedef struct {
    size_t n, dim, loaded, upper_slots;
    int entry, max_level;
    float *vectors;
    int64_t *ids;
    uint32_t *base, *upper, *offset, *seen;
    unsigned char *levels, *degree, *upper_degree;
    GHit *frontier, *results;
    uint32_t epoch;
} MVGraph;

static int gh_less(GHit a, GHit b) { return a.d < b.d || (a.d == b.d && a.id < b.id); }
static int gh_compare(const void *pa, const void *pb) {
    GHit a=*(const GHit*)pa,b=*(const GHit*)pb;
    return gh_less(a,b)?-1:gh_less(b,a)?1:0;
}
static void gh_push(GHit *heap,size_t *n,GHit value,int maximum) {
    size_t at=(*n)++;
    while(at) {
        size_t parent=(at-1)/2;
        if(!(maximum?gh_less(heap[parent],value):gh_less(value,heap[parent])))break;
        heap[at]=heap[parent];at=parent;
    }
    heap[at]=value;
}
static GHit gh_pop(GHit *heap,size_t *n,int maximum) {
    GHit result=heap[0],value=heap[--*n];size_t at=0;
    while(at*2+1<*n) {
        size_t child=at*2+1;
        if(child+1<*n && (maximum?gh_less(heap[child],heap[child+1]):gh_less(heap[child+1],heap[child])))++child;
        if(!(maximum?gh_less(value,heap[child]):gh_less(heap[child],value)))break;
        heap[at]=heap[child];at=child;
    }
    if(*n)heap[at]=value;
    return result;
}
void mv_graph_free(MVGraph *g) {
    if(!g)return;
    free(g->vectors);free(g->ids);free(g->base);free(g->upper);free(g->offset);
    free(g->seen);free(g->levels);free(g->degree);free(g->upper_degree);
    free(g->frontier);free(g->results);free(g);
}
MVGraph *mv_graph_new(size_t n,size_t dim,size_t budget) {
    if(n<1024 || n>20000 || dim<16 || dim>256)return NULL;
    size_t lower=n*(dim*4+8+GM0*4+4+4+1+1+sizeof(GHit))+GMAXEF*sizeof(GHit)+sizeof(MVGraph)+(128u<<10);
    if(lower>budget)return NULL;
    MVGraph *g=calloc(1,sizeof(*g));if(!g)return NULL;
    g->n=n;g->dim=dim;g->entry=-1;g->max_level=-1;
    g->levels=calloc(n,1);g->offset=calloc(n,sizeof(uint32_t));
    if(!g->levels || !g->offset){mv_graph_free(g);return NULL;}
    uint32_t random=0x73b19ae5u;
    for(size_t i=0;i<n;++i) {
        g->offset[i]=(uint32_t)g->upper_slots;
        for(unsigned level=0;level<GMAXLEVEL;++level) {
            random^=random<<13;random^=random>>17;random^=random<<5;
            if(random&15u)break;
            ++g->levels[i];++g->upper_slots;
        }
    }
    if(lower+g->upper_slots*(GM*4+1)>budget){mv_graph_free(g);return NULL;}
    g->vectors=malloc(n*dim*sizeof(float));g->ids=malloc(n*sizeof(int64_t));
    g->base=calloc(n*GM0,sizeof(uint32_t));g->degree=calloc(n,1);g->seen=calloc(n,sizeof(uint32_t));
    g->upper=calloc(g->upper_slots?g->upper_slots*GM:1,sizeof(uint32_t));
    g->upper_degree=calloc(g->upper_slots?g->upper_slots:1,1);
    g->frontier=malloc(n*sizeof(GHit));g->results=malloc(GMAXEF*sizeof(GHit));
    if(!g->vectors || !g->ids || !g->base || !g->degree || !g->seen || !g->upper || !g->upper_degree || !g->frontier || !g->results) {
        mv_graph_free(g);return NULL;
    }
    return g;
}
int mv_graph_append(MVGraph *g,const unsigned char *data,size_t bytes,const unsigned char *mask) {
    if(!g || !data)return 0;
    size_t stride=8+g->dim*4;if(bytes%stride)return 0;
    for(size_t i=0;i<bytes/stride;++i) {
        if(mask && !mask[i])continue;
        if(g->loaded==g->n)return 0;
        memcpy(g->ids+g->loaded,data+i*stride,8);
        memcpy(g->vectors+g->loaded*g->dim,data+i*stride+8,g->dim*4);
        ++g->loaded;
    }
    return 1;
}
static uint32_t *g_edges(MVGraph *g,uint32_t id,unsigned level,unsigned char **degree) {
    if(!level){*degree=g->degree+id;return g->base+(size_t)id*GM0;}
    size_t slot=g->offset[id]+level-1;
    *degree=g->upper_degree+slot;return g->upper+slot*GM;
}
static double g_distance(MVGraph *g,uint32_t id,const double *q,distance_fn fn) {
    return fn((const unsigned char*)(g->vectors+(size_t)id*g->dim),q,g->dim,INFINITY);
}
static uint32_t g_greedy(MVGraph *g,const double *q,uint32_t ep,unsigned level,size_t limit,distance_fn fn) {
    GHit best={g_distance(g,ep,q,fn),ep};
    for(;;) {
        uint32_t previous=best.id;unsigned char *degree;
        uint32_t *edges=g_edges(g,previous,level,&degree);
        for(unsigned i=0;i<*degree;++i)if(edges[i]<limit) {
            GHit hit={g_distance(g,edges[i],q,fn),edges[i]};
            if(gh_less(hit,best))best=hit;
        }
        if(best.id==previous)return best.id;
    }
}
static size_t g_search(MVGraph *g,const double *q,uint32_t ep,unsigned level,size_t ef,size_t limit,distance_fn fn) {
    if(++g->epoch==0){memset(g->seen,0,g->n*sizeof(uint32_t));g->epoch=1;}
    size_t front=0,results=0;
    GHit start={g_distance(g,ep,q,fn),ep};g->seen[ep]=g->epoch;
    gh_push(g->frontier,&front,start,0);gh_push(g->results,&results,start,1);
    while(front) {
        GHit current=gh_pop(g->frontier,&front,0);
        if(results==ef && current.d>g->results[0].d)break;
        unsigned char *degree;uint32_t *edges=g_edges(g,current.id,level,&degree);
        for(unsigned i=0;i<*degree;++i) {
            uint32_t id=edges[i];if(id>=limit || g->seen[id]==g->epoch)continue;
            g->seen[id]=g->epoch;GHit hit={g_distance(g,id,q,fn),id};
            if(results<ef || gh_less(hit,g->results[0])) {
                gh_push(g->frontier,&front,hit,0);
                if(results==ef)gh_pop(g->results,&results,1);
                gh_push(g->results,&results,hit,1);
            }
        }
    }
    qsort(g->results,results,sizeof(GHit),gh_compare);return results;
}
static size_t g_neighbors(MVGraph *g,GHit *candidates,size_t count,size_t cap,uint32_t *chosen,distance_fn fn) {
    size_t selected=0;unsigned char rejected[GEF+GM0+1]={0};
    double q[256];
    for(size_t i=0;i<count && selected<cap;++i) {
        const float *v=g->vectors+(size_t)candidates[i].id*g->dim;
        for(size_t j=0;j<g->dim;++j)q[j]=v[j];
        int useful=1;
        for(size_t j=0;j<selected;++j)if(g_distance(g,chosen[j],q,fn)<candidates[i].d){useful=0;break;}
        if(useful)chosen[selected++]=candidates[i].id;else rejected[i]=1;
    }
    /* Fill unused degree slots with nearest rejected candidates. */
    for(size_t i=0;i<count && selected<cap;++i)if(rejected[i])chosen[selected++]=candidates[i].id;
    return selected;
}
static void g_connect(MVGraph *g,uint32_t center,uint32_t other,unsigned level,distance_fn fn) {
    unsigned char *degree;uint32_t *edges=g_edges(g,center,level,&degree);
    size_t cap=level?GM:GM0;
    if(*degree<cap){edges[(*degree)++]=other;return;}
    GHit candidates[GM0+1];uint32_t chosen[GM0];double q[256];
    for(size_t j=0;j<g->dim;++j)q[j]=g->vectors[(size_t)center*g->dim+j];
    for(size_t i=0;i<cap;++i)candidates[i]=(GHit){g_distance(g,edges[i],q,fn),edges[i]};
    candidates[cap]=(GHit){g_distance(g,other,q,fn),other};
    qsort(candidates,cap+1,sizeof(GHit),gh_compare);
    *degree=(unsigned char)g_neighbors(g,candidates,cap+1,cap,chosen,fn);
    memcpy(edges,chosen,*degree*sizeof(uint32_t));
}
int mv_graph_build(MVGraph *g) {
    if(!g || g->loaded!=g->n)return 0;
    /* Identical sampled vectors are a poor graph workload; exact scan is safer. */
    distance_fn fn=choose_distance(g->dim,g->n);
    double q[256];uint32_t chosen[GM0];
    for(size_t j=0;j<g->dim;++j)q[j]=g->vectors[j];
    int varied=0;
    for(size_t i=1;i<9;++i)if(g_distance(g,(uint32_t)(g->n*i/9),q,fn)>0){varied=1;break;}
    if(!varied)return 0;
    for(uint32_t id=0;id<g->n;++id) {
        if(g->entry<0){g->entry=(int)id;g->max_level=g->levels[id];continue;}
        for(size_t j=0;j<g->dim;++j)q[j]=g->vectors[(size_t)id*g->dim+j];
        uint32_t ep=(uint32_t)g->entry;
        for(int level=g->max_level;level>(int)g->levels[id];--level)ep=g_greedy(g,q,ep,(unsigned)level,id,fn);
        int top=g->levels[id]<g->max_level?g->levels[id]:g->max_level;
        for(int level=top;level>=0;--level) {
            size_t count=g_search(g,q,ep,(unsigned)level,GEF,id,fn);
            ep=g->results[0].id;
            size_t used=g_neighbors(g,g->results,count,level?GM:GM0,chosen,fn);
            unsigned char *degree;uint32_t *edges=g_edges(g,id,(unsigned)level,&degree);
            *degree=(unsigned char)used;memcpy(edges,chosen,used*sizeof(uint32_t));
            for(size_t i=0;i<used;++i)g_connect(g,chosen[i],id,(unsigned)level,fn);
        }
        if((int)g->levels[id]>g->max_level){g->entry=(int)id;g->max_level=g->levels[id];}
    }
    return 1;
}
size_t mv_graph_search(MVGraph *g,const double *q,size_t k,size_t ef,int64_t *ids) {
    if(!g || g->entry<0 || !q || !ids || !k || k>64 || ef<k+1 || ef>GMAXEF)return SIZE_MAX;
    if(ef>g->n)ef=g->n;
    distance_fn fn=choose_distance(g->dim,g->n);
    double low=INFINITY, high=0;
    for(size_t i=0;i<16;++i) {
        double d=g_distance(g,(uint32_t)(g->n*i/16),q,fn);
        if(d<low)low=d;if(d>high)high=d;
    }
    if(high-low<.05*high)return SIZE_MAX;
    uint32_t ep=(uint32_t)g->entry;
    for(int level=g->max_level;level>0;--level)ep=g_greedy(g,q,ep,(unsigned)level,g->n,fn);
    size_t count=g_search(g,q,ep,0,ef,g->n,fn);
    if(count<ef || count<=k)return SIZE_MAX;
    double error=128*DBL_EPSILON*(g->dim+1);
    for(size_t i=1;i<=k;++i)
        if(fabs(g->results[i].d-g->results[i-1].d)<=error*fmax(g->results[i].d,g->results[i-1].d)+DBL_MIN)return SIZE_MAX;
    /* Concentrated distances (e.g. a zero query on normalized data) are OOD. */
    if(g->results[count-1].d-g->results[0].d<.05*g->results[count-1].d)return SIZE_MAX;
    for(size_t i=0;i<k;++i)ids[i]=g->ids[g->results[i].id];
    return k;
}
