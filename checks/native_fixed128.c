#include "../native/screen.c"
#include <assert.h>
#include <stdio.h>
static uint32_t state=872909;
static uint32_t next(void){state^=state<<13;state^=state>>17;state^=state<<5;return state;}
int main(void){
    if(!mv_screen_simd()){puts("{\"status\":\"UNSUPPORTED_AVX2\"}");return 0;}
    unsigned char bytes[128*4+8];double query[128];size_t checks=0;
    for(size_t trial=0;trial<2560;trial++){
        unsigned char *data=bytes+trial%8;
        for(size_t i=0;i<128;i++){
            int exponent=(int)(next()%270)-149;
            float value=ldexpf((next()&1)?1.25f:-1.5f,exponent);
            float q=ldexpf((next()&1)?1.0f:-1.0f,exponent);
            memcpy(data+i*4,&value,4);float_query[i]=q;query[i]=q;
        }
        double score=avx2_float_distance(data,query,128,INFINITY);
        double cuts[4]={INFINITY,score,nextafter(score,0),nextafter(score,INFINITY)};
        for(size_t j=0;j<4;j++){
            double a=avx2_float_distance(data,query,128,cuts[j]);
            double b=avx2_float_distance128(data,query,128,cuts[j]);
            assert(!memcmp(&a,&b,sizeof(a)));++checks;
        }
    }
    mv_screen_set_simd(0);assert(!mv_screen_simd());
    printf("{\"status\":\"PASS\",\"bitwise_scores_and_cutoffs\":%zu}\n",checks);
}
