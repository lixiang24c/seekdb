#define _POSIX_C_SOURCE 200809L
#include <stdio.h>
#include <stdlib.h>
#include <assert.h>
#include "../native/screen.c"
static uint64_t rng=42;
static double sample(void){rng=rng*6364136223846793005ULL+1;return (double)(rng>>11)/9007199254740992.0*2-1;}
__attribute__((target("avx2"))) static double estimate(const unsigned char *p,const double *q){return quant_distance128(p,q,128,INFINITY);}
int main(void){
    mv_screen_set_simd(1);mv_screen_set_float(1);
    if(!mv_quant_available()){puts("{\"status\":\"UNSUPPORTED_CPU\",\"bounds\":0}");return 0;}
    unsigned char rows[520],packed[144];double q[128];size_t checked=0,admitted=0;
    for(size_t trial=0;trial<12000;trial++){
        float x[128];double magnitude=trial%6==0?1e-40:trial%6==1?1e30:trial%6==2?0:trial%6==3?1e-5:1;
        for(size_t j=0;j<128;j++){x[j]=(float)(sample()*magnitude);q[j]=(double)(float)(sample()*magnitude);}
        if(trial%7==0)for(size_t j=0;j<128;j++)q[j]=nextafterf(x[j],INFINITY);
        memcpy(rows+8,x,512);double norm=0,error=0;assert(mv_quant_pack(rows,1,packed,&norm,&error));
        admitted+=mv_quant_prepare(q,norm,error,rows+8)!=0;
        long double exact=0;for(size_t j=0;j<128;j++){long double d=(long double)x[j]-q[j];exact+=d*d;}
        double predicted=estimate(packed,q);
        assert(fabsl((long double)predicted-exact)<=(long double)quant_delta);++checked;
    }
    mv_screen_set_simd(0);assert(!mv_quant_available());
    printf("{\"status\":\"PASS\",\"bounds\":%zu,\"admitted\":%zu,\"scalar_disables_quant\":true}\n",checked,admitted);
}
