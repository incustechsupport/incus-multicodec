/* aptX / aptX HD encoder DLLs around Qualcomm's encoders from the Android Open Source Project
 * (packages/modules/Bluetooth/system/embdrv/encoder_for_aptx{,hd}, Apache License 2.0).
 *
 * Built twice by build.cmd: aptx.dll (classic aptX) and, with APTX_HD defined, aptxhd.dll. The two
 * encoders share internal symbol names, so they can't be linked into one DLL.
 *
 * Plain C API for ctypes (btmulticodec/aptx.py):
 *   void  *aptx_init(void);
 *   void   aptx_reset(void *ctx);
 *   void   aptx_finish(void *ctx);
 *   size_t aptx_encode(void *ctx, const uint8_t *in, size_t in_size, uint8_t *out, size_t out_size,
 *                      size_t *written);
 * Input is packed signed 24-bit little-endian stereo PCM, encoded in groups of 4 stereo frames (24
 * bytes); each group gives 4 bytes (aptX) or 6 bytes (aptX HD), the left and right codewords
 * big-endian, exactly as Android's a2dp_vendor_aptx(_hd)_encoder.cc lays them out. aptx_encode
 * returns the input bytes consumed (whole groups that fit in the output buffer).
 */

#include <stdint.h>
#include <stdlib.h>

#ifdef APTX_HD
#include "aptXHDbtenc.h"
#define ENC_SIZEOF SizeofAptxhdbtenc
#define ENC_INIT aptxhdbtenc_init
#define ENC_STEREO aptxhdbtenc_encodestereo
#define GROUP_OUT 6
#else
#include "aptXbtenc.h"
#define ENC_SIZEOF SizeofAptxbtenc
#define ENC_INIT aptxbtenc_init
#define ENC_STEREO aptxbtenc_encodestereo
#define GROUP_OUT 4
#endif

#define API __declspec(dllexport)
#define GROUP_IN 24 /* 4 frames x 2 channels x 3 bytes */

static int32_t s24(const uint8_t *p) {
    return (int32_t)((uint32_t)p[0] | ((uint32_t)p[1] << 8) | ((uint32_t)(int8_t)p[2] << 16));
}

API void *aptx_init(void) {
    void *state = calloc(1, (size_t)ENC_SIZEOF());
    if (state != NULL && ENC_INIT(state, 0) != 0) {
        free(state);
        state = NULL;
    }
    return state;
}

API void aptx_reset(void *ctx) {
    if (ctx != NULL) {
        ENC_INIT(ctx, 0);
    }
}

API void aptx_finish(void *ctx) {
    free(ctx);
}

API size_t aptx_encode(void *ctx, const uint8_t *in, size_t in_size, uint8_t *out, size_t out_size,
                       size_t *written) {
    size_t groups = in_size / GROUP_IN;
    size_t g, i;
    if (groups > out_size / GROUP_OUT) {
        groups = out_size / GROUP_OUT;
    }
    for (g = 0; g < groups; g++) {
        const uint8_t *p = in + g * GROUP_IN;
        uint8_t *o = out + g * GROUP_OUT;
        int32_t left[4], right[4];
        for (i = 0; i < 4; i++) {
#ifdef APTX_HD
            left[i] = s24(p + i * 6);
            right[i] = s24(p + i * 6 + 3);
#else
            /* aptX codes 16-bit audio: the top 16 bits of each 24-bit sample */
            left[i] = s24(p + i * 6) >> 8;
            right[i] = s24(p + i * 6 + 3) >> 8;
#endif
        }
#ifdef APTX_HD
        {
            uint32_t code[2];
            ENC_STEREO(ctx, left, right, code);
            for (i = 0; i < 2; i++) {
                o[i * 3 + 0] = (uint8_t)(code[i] >> 16);
                o[i * 3 + 1] = (uint8_t)(code[i] >> 8);
                o[i * 3 + 2] = (uint8_t)code[i];
            }
        }
#else
        {
            uint16_t code[2];
            ENC_STEREO(ctx, left, right, code);
            for (i = 0; i < 2; i++) {
                o[i * 2 + 0] = (uint8_t)(code[i] >> 8);
                o[i * 2 + 1] = (uint8_t)code[i];
            }
        }
#endif
    }
    *written = groups * GROUP_OUT;
    return groups * GROUP_IN;
}
