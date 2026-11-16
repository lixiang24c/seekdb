#ifndef MV_VECTOR_CODEC_H
#define MV_VECTOR_CODEC_H
#include <stdint.h>
#include <stddef.h>
#define MV_CODEC_PACKED UINT64_C(0x8000000000000000)
#define MV_CODEC_OFFSET UINT64_C(0x7fffffffffffffff)
#ifdef __cplusplus
extern "C" {
#endif
size_t mv_codec_width(size_t dimension,uint32_t descriptor);
int mv_codec_encode(const unsigned char *raw,size_t dimension,uint32_t descriptor,unsigned char *packed);
int mv_codec_decode(const unsigned char *packed,size_t dimension,unsigned char *raw);
int mv_codec_test(const unsigned char *raw,size_t dimension,uint32_t descriptor);
#ifdef __cplusplus
}
#endif
#endif
