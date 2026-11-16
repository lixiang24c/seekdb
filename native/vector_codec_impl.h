/* Lossless record codecs. Encoding is validated against stored binary32 bits. */
#include "vector_codec.h"
#include <fenv.h>
static const double mv_codec_scale[7]={1,10,100,1000,10000,100000,1000000};
static const double mv_codec_inverse[7]={1,.1,.01,.001,.0001,.00001,.000001};
static size_t mv_codec_exceptions(size_t dim){return (dim+31)/32>4?(dim+31)/32:4;}
size_t mv_codec_width(size_t dim,uint32_t descriptor){
 if(!descriptor)return dim<=SIZE_MAX/4?dim*4:SIZE_MAX;
 unsigned mode=descriptor&255,scale=(descriptor>>8)&255;
 if(!dim||dim>4096||(descriptor>>24)!=1||(descriptor&0x00ff0000)||scale>6||mode<1||mode>3||(mode==3&&scale))return SIZE_MAX;
 return mode==1?4+dim*3:mode==2?4+dim*2+mv_codec_exceptions(dim)*4:4+dim*2;
}
static uint32_t mv_f32_bits(float f){uint32_t b;memcpy(&b,&f,4);return b;}
static float mv_codec_restore(int32_t code,double factor,int32_t negative_zero){
 if(code==negative_zero){uint32_t b=0x80000000;float f;memcpy(&f,&b,4);return f;}
 return (float)((double)code*factor);
}
static int mv_codec_decimal(uint32_t b,double scale,double factor,int mode,int32_t *code){
 if(b==0x80000000){*code=mode==1?-8388608:-32767;return 1;}
 if((b&0x7f800000)==0x7f800000)return 0;
 float x;memcpy(&x,&b,4);double scaled=(double)x*scale;
 int32_t low=mode==1?-8388607:-32766,high=mode==1?8388607:32767;
 if(scaled<(double)low-.5||scaled>(double)high+.5)return 0;
 int32_t c=(int32_t)(scaled+(scaled>=0?.5:-.5));if(c<low||c>high)return 0;
 if(mv_f32_bits(mv_codec_restore(c,factor,mode==1?-8388608:-32767))!=b)return 0;*code=c;return 1;
}
static uint32_t mv_half_bits(uint16_t h){
 uint32_t sign=(uint32_t)(h&0x8000)<<16,exp=(h>>10)&31,mant=h&1023;
 if(!exp){if(!mant)return sign;unsigned top=31-(unsigned)__builtin_clz(mant);return sign|((top+103)<<23)|((mant-(1u<<top))<<(23-top));}
 if(exp==31){if(mant)mant|=512;return sign|0x7f800000|(mant<<13);}
 return sign|((exp+112)<<23)|(mant<<13);
}
static int mv_half_exact(uint32_t b,uint16_t *out){
 uint16_t sign=(uint16_t)((b>>16)&0x8000);uint32_t exp=(b>>23)&255,mant=b&0x7fffff;
 if(!exp&&!mant){*out=sign;return 1;}if(exp<103||exp>142)return 0;
 if(exp>=113){if(mant&8191)return 0;*out=sign|(uint16_t)((exp-112)<<10)|(uint16_t)(mant>>13);return 1;}
 unsigned shift=126-exp;uint32_t sig=mant|0x800000;if(sig&((1u<<shift)-1))return 0;*out=sign|(uint16_t)(sig>>shift);return 1;
}
__attribute__((target("avx2,fma")))
static int mv_decimal16_encode_simd(const unsigned char*,size_t,unsigned,unsigned char*);
int mv_codec_encode(const unsigned char *raw,size_t dim,uint32_t descriptor,unsigned char *out){
 if(!raw||!out||!descriptor||mv_codec_width(dim,descriptor)==SIZE_MAX||fegetround()!=FE_TONEAREST)return 0;
 unsigned mode=descriptor&255,exponent=(descriptor>>8)&255;memcpy(out,&descriptor,4);size_t exceptions=0,slots=mv_codec_exceptions(dim);
 if(mode==2){memset(out+4+dim*2,0,slots*4);if(mv_screen_simd()&&__builtin_cpu_supports("fma"))return mv_decimal16_encode_simd(raw,dim,exponent,out+4);}
 for(size_t j=0;j<dim;j++){uint32_t b;memcpy(&b,raw+j*4,4);if((b&0x7f800000)==0x7f800000)return 0;
  if(mode==3){uint16_t h;if(!mv_half_exact(b,&h))return 0;memcpy(out+4+j*2,&h,2);continue;}
  int32_t code;if(!mv_codec_decimal(b,mv_codec_scale[exponent],mv_codec_inverse[exponent],mode,&code)){
   if(mode!=2||exceptions==slots)return 0;code=-32768;memcpy(out+4+dim*2+exceptions*4,&b,4);++exceptions;
  }
  uint32_t value=(uint32_t)code;out[4+j*(mode==1?3:2)]=(unsigned char)value;out[5+j*(mode==1?3:2)]=(unsigned char)(value>>8);if(mode==1)out[6+j*3]=(unsigned char)(value>>16);
 }return 1;
}
int mv_codec_test(const unsigned char *raw,size_t dim,uint32_t descriptor){unsigned char buffer[4+4096*4];return mv_codec_encode(raw,dim,descriptor,buffer);}
__attribute__((target("avx,f16c")))
static void mv_half_decode_simd(const unsigned char *p,size_t dim,unsigned char *out){
 size_t j=0;for(;j+8<=dim;j+=8){__m256 f=_mm256_cvtph_ps(_mm_loadu_si128((const __m128i*)(p+j*2)));memcpy(out+j*4,&f,32);}
 for(;j<dim;j++){uint16_t h;memcpy(&h,p+j*2,2);uint32_t b=mv_half_bits(h);memcpy(out+j*4,&b,4);}
}
__attribute__((target("avx2")))
static int mv_decimal_decode_simd(const unsigned char *p,size_t dim,int mode,double factor,unsigned char *out){
 size_t j=0,exceptions=0,slots=mv_codec_exceptions(dim);const __m128i shuffle=_mm_setr_epi8(0,1,2,-128,3,4,5,-128,6,7,8,-128,9,10,11,-128);
 for(;j+4<=dim;j+=4){__m128i codes;
  if(mode==1){uint32_t hi;memcpy(&hi,p+j*3+8,4);codes=_mm_or_si128(_mm_loadl_epi64((const __m128i*)(p+j*3)),_mm_slli_si128(_mm_cvtsi32_si128((int)hi),8));codes=_mm_srai_epi32(_mm_slli_epi32(_mm_shuffle_epi8(codes,shuffle),8),8);}
  else codes=_mm_cvtepi16_epi32(_mm_loadl_epi64((const __m128i*)(p+j*2)));
  __m128 f=_mm256_cvtpd_ps(_mm256_mul_pd(_mm256_cvtepi32_pd(codes),_mm256_set1_pd(factor)));
  f=_mm_blendv_ps(f,_mm_castsi128_ps(_mm_set1_epi32(INT32_MIN)),_mm_castsi128_ps(_mm_cmpeq_epi32(codes,_mm_set1_epi32(mode==1?-8388608:-32767))));memcpy(out+j*4,&f,16);
  if(mode==2){unsigned mask=(unsigned)_mm_movemask_ps(_mm_castsi128_ps(_mm_cmpeq_epi32(codes,_mm_set1_epi32(-32768))));while(mask){unsigned lane=__builtin_ctz(mask);mask&=mask-1;if(exceptions==slots)return 0;memcpy(out+(j+lane)*4,p+dim*2+exceptions*4,4);++exceptions;}}
 }
 for(;j<dim;j++){uint32_t u=(uint32_t)p[j*(mode==1?3:2)]|((uint32_t)p[j*(mode==1?3:2)+1]<<8);int32_t code;
  if(mode==1){u|=(uint32_t)p[j*3+2]<<16;code=(int32_t)(u^0x800000)-0x800000;}else code=(int32_t)(u^0x8000)-0x8000;
  if(mode==2&&code==-32768){if(exceptions==slots)return 0;memcpy(out+j*4,p+dim*2+exceptions*4,4);++exceptions;}else{float f=mv_codec_restore(code,factor,mode==1?-8388608:-32767);memcpy(out+j*4,&f,4);}
 }return 1;
}
/* Split the decimal coefficient into two binary32 terms. The encoder still
   validates against the authoritative binary64 reconstruction. Exhaustive
   signed16/scale tests gate this faster decoder before engine integration. */
__attribute__((target("avx2,fma")))
static int mv_decimal16_decode_fma(const unsigned char *p,size_t dim,double factor,unsigned char *out){
 float high=(float)factor,low=(float)(factor-(double)high);__m256 hi=_mm256_set1_ps(high),lo=_mm256_set1_ps(low);size_t j=0,exceptions=0,slots=mv_codec_exceptions(dim);
 for(;j+8<=dim;j+=8){
  __m256i codes=_mm256_cvtepi16_epi32(_mm_loadu_si128((const __m128i*)(p+j*2)));__m256 x=_mm256_cvtepi32_ps(codes);
  __m256 f=_mm256_fmadd_ps(x,hi,_mm256_mul_ps(x,lo));
  f=_mm256_blendv_ps(f,_mm256_castsi256_ps(_mm256_set1_epi32(INT32_MIN)),_mm256_castsi256_ps(_mm256_cmpeq_epi32(codes,_mm256_set1_epi32(-32767))));_mm256_storeu_ps((float*)(out+j*4),f);
  unsigned mask=(unsigned)_mm256_movemask_ps(_mm256_castsi256_ps(_mm256_cmpeq_epi32(codes,_mm256_set1_epi32(-32768))));
  while(mask){unsigned lane=__builtin_ctz(mask);mask&=mask-1;if(exceptions==slots)return 0;memcpy(out+(j+lane)*4,p+dim*2+exceptions*4,4);++exceptions;}
 }
 for(;j<dim;j++){int16_t code;memcpy(&code,p+j*2,2);if(code==-32768){if(exceptions==slots)return 0;memcpy(out+j*4,p+dim*2+exceptions*4,4);++exceptions;}else{float f=mv_codec_restore(code,factor,-32767);memcpy(out+j*4,&f,4);}}
 return 1;
}
int mv_codec_decode(const unsigned char *payload,size_t dim,unsigned char *out){
 if(!payload||!out)return 0;uint32_t descriptor;memcpy(&descriptor,payload,4);if(!descriptor||mv_codec_width(dim,descriptor)==SIZE_MAX)return 0;
 unsigned mode=descriptor&255,scale=(descriptor>>8)&255;const unsigned char *p=payload+4;
 if(mode==3&&mv_screen_simd()&&__builtin_cpu_supports("f16c")){mv_half_decode_simd(p,dim,out);return 1;}
 if(mode==2&&mv_screen_simd()&&__builtin_cpu_supports("fma"))return mv_decimal16_decode_fma(p,dim,mv_codec_inverse[scale],out);
 if(mode!=3&&mv_screen_simd())return mv_decimal_decode_simd(p,dim,mode,mv_codec_inverse[scale],out);
 size_t exceptions=0,slots=mv_codec_exceptions(dim);
 for(size_t j=0;j<dim;j++){
  uint32_t u=(uint32_t)p[j*(mode==1?3:2)]|((uint32_t)p[j*(mode==1?3:2)+1]<<8);
  if(mode==3){uint32_t b=mv_half_bits((uint16_t)u);memcpy(out+j*4,&b,4);continue;}
  int32_t c;if(mode==1){u|=(uint32_t)p[j*3+2]<<16;c=(int32_t)(u^0x800000)-0x800000;}else c=(int32_t)(u^0x8000)-0x8000;
  if(mode==2&&c==-32768){if(exceptions==slots)return 0;memcpy(out+j*4,p+dim*2+exceptions*4,4);++exceptions;}else{float f=mv_codec_restore(c,mv_codec_inverse[scale],mode==1?-8388608:-32767);memcpy(out+j*4,&f,4);}
 }return 1;
}

__attribute__((target("avx2,fma")))
static int mv_decimal16_encode_simd(const unsigned char *raw,size_t dim,unsigned exponent,unsigned char *p){
 double factor=mv_codec_inverse[exponent];float high=(float)factor,low=(float)(factor-(double)high);__m256 hi=_mm256_set1_ps(high),lo=_mm256_set1_ps(low),scale=_mm256_set1_ps((float)mv_codec_scale[exponent]);size_t j=0,exceptions=0,slots=mv_codec_exceptions(dim);
 for(;j+8<=dim;j+=8){
  __m256 x=_mm256_loadu_ps((const float*)(raw+j*4));__m256i bits=_mm256_castps_si256(x),codes=_mm256_cvtps_epi32(_mm256_mul_ps(x,scale));
  __m256 integer=_mm256_cvtepi32_ps(codes),restored=_mm256_fmadd_ps(integer,hi,_mm256_mul_ps(integer,lo));
  __m256i negative_zero=_mm256_cmpeq_epi32(bits,_mm256_set1_epi32(INT32_MIN));
  __m256i valid=_mm256_and_si256(_mm256_cmpeq_epi32(bits,_mm256_castps_si256(restored)),_mm256_and_si256(_mm256_cmpgt_epi32(codes,_mm256_set1_epi32(-32767)),_mm256_cmpgt_epi32(_mm256_set1_epi32(32768),codes)));
  valid=_mm256_or_si256(valid,negative_zero);codes=_mm256_blendv_epi8(codes,_mm256_set1_epi32(-32767),negative_zero);
  unsigned bad=(~(unsigned)_mm256_movemask_ps(_mm256_castsi256_ps(valid)))&255;
  if(bad){
   __m256i finite=_mm256_cmpeq_epi32(_mm256_and_si256(bits,_mm256_set1_epi32(0x7f800000)),_mm256_set1_epi32(0x7f800000));if(_mm256_movemask_ps(_mm256_castsi256_ps(finite)))return 0;
   codes=_mm256_blendv_epi8(_mm256_set1_epi32(-32768),codes,valid);
   while(bad){unsigned lane=__builtin_ctz(bad);bad&=bad-1;if(exceptions==slots)return 0;memcpy(p+dim*2+exceptions*4,raw+(j+lane)*4,4);++exceptions;}
  }
  __m128i packed=_mm_packs_epi32(_mm256_castsi256_si128(codes),_mm256_extracti128_si256(codes,1));_mm_storeu_si128((__m128i*)(p+j*2),packed);
 }
 for(;j<dim;j++){uint32_t b;memcpy(&b,raw+j*4,4);if((b&0x7f800000)==0x7f800000)return 0;int32_t code;if(!mv_codec_decimal(b,mv_codec_scale[exponent],factor,2,&code)){if(exceptions==slots)return 0;code=-32768;memcpy(p+dim*2+exceptions*4,&b,4);++exceptions;}int16_t c=(int16_t)code;memcpy(p+j*2,&c,2);}
 return 1;
}
