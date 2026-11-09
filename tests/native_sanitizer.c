#include <assert.h>
#include <math.h>
#include <stdio.h>
#include "../native/topk.c"
static int compare(const void *a,const void *b) {
    Hit x=*(const Hit*)a,y=*(const Hit*)b;
    return less(x,y)?-1:less(y,x)?1:0;
}
int main(void) {
    const size_t dimensions[]={1,3,4,7,128,131};
    for (size_t t=0;t<6;t++) {
        size_t dim=dimensions[t],count=2053,stride=16+4*dim;
        unsigned char *rows=calloc(count,stride),*mask=malloc(count);
        double *query=calloc(dim,8),*distance=malloc(count*8);
        int64_t *keys=malloc(count*8);uint64_t *offsets=malloc(count*8);
        Hit *truth=malloc(count*sizeof(Hit));assert(rows&&mask&&query&&distance&&keys&&offsets&&truth);
        uint32_t *positions=malloc(count*sizeof(uint32_t));assert(positions);
        size_t expected=0;
        for(size_t i=0;i<count;i++) {
            int64_t pk=(int64_t)(count-i);uint64_t offset=i*113;
            memcpy(rows+i*stride,&pk,8);memcpy(rows+i*stride+8,&offset,8);
            float *v=(float*)(rows+i*stride+16);
            for(size_t j=0;j<dim;j++)v[j]=(float)((int)((i*17+j*3)%31)-15)/4;
            mask[i]=(i%3)!=0;
            if(mask[i]) {
                positions[expected]=(uint32_t)i;
                truth[expected++]=(Hit){scalar_l2(v,query,dim),pk,offset};
            }
        }
        qsort(truth,expected,sizeof(Hit),compare);
        unsigned char *codes=malloc(count*dim);double minimum,step;assert(codes);
        assert(mv_quantize(rows,count,stride,dim,&minimum,&step,codes));
        size_t choices[]={0,1,10,count+3};
        for(int simd=0;simd<2;simd++)for(size_t c=0;c<4;c++)for(int mode=0;mode<4;mode++) {
            size_t k=choices[c],got=mode>=2?
                mv_topk_quantized(rows,count,stride,dim,query,codes,minimum,step,
                    mode==3?(const void*)positions:(const void*)mask,expected,mode==3,k,simd,distance,keys,offsets):mode==1?
                mv_topk_selected(rows,count,stride,dim,query,positions,expected,k,simd,distance,keys,offsets):
                mv_topk(rows,count,stride,dim,query,mask,k,simd,distance,keys,offsets);
            assert(got==(k<expected?k:expected));
            for(size_t i=0;i<got;i++)assert(keys[i]==truth[i].key&&offsets[i]==truth[i].offset&&distance[i]==truth[i].distance);
        }
        free(codes);free(positions);free(truth);free(rows);free(mask);free(query);free(distance);free(keys);free(offsets);
    }
    puts("PASS: native scalar/SIMD sanitizer checks, odd dimensions, masks, ties and K bounds");return 0;
}
