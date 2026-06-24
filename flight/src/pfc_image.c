/* pfc_image.c — 2-D MED (LOCO-I/JPEG-LS) predictor front-end.
 * SPDX-License-Identifier: Apache-2.0
 *
 * The plane is coded in horizontal bands. Each band predicts only from samples WITHIN the band
 * (the first row/column use the mid-value default), so every band is independently decodable —
 * a corrupted band cannot corrupt its neighbours (R6). Integer-only, deterministic.
 */
#include "pfc_internal.h"

static uint32_t px_get(const void *p, uint8_t bd, size_t i)
{
    return (bd > 8u) ? (uint32_t)((const uint16_t *)p)[i]
                     : (uint32_t)((const uint8_t *)p)[i];
}

static void px_set(void *p, uint8_t bd, size_t i, uint32_t v)
{
    if (bd > 8u) {
        ((uint16_t *)p)[i] = (uint16_t)v;
    } else {
        ((uint8_t *)p)[i] = (uint8_t)v;
    }
}

/* MED predictor over already-available neighbours within band rows [y0,y1). */
static uint32_t med_predict(const void *p, uint8_t bd, uint32_t width,
                            uint32_t x, uint32_t y, uint32_t y0, uint32_t mid)
{
    uint32_t a, b, c, lo, hi;
    if ((y == y0) && (x == 0u)) {
        return mid;
    }
    if (y == y0) {
        return px_get(p, bd, (size_t)y * width + (x - 1u));        /* left only */
    }
    if (x == 0u) {
        return px_get(p, bd, (size_t)(y - 1u) * width + x);        /* up only */
    }
    a = px_get(p, bd, (size_t)y * width + (x - 1u));               /* left  */
    b = px_get(p, bd, (size_t)(y - 1u) * width + x);              /* up    */
    c = px_get(p, bd, (size_t)(y - 1u) * width + (x - 1u));       /* up-left */
    lo = (a < b) ? a : b;
    hi = (a > b) ? a : b;
    if (c >= hi) {
        return lo;
    }
    if (c <= lo) {
        return hi;
    }
    return a + b - c;
}

void pfc_image_encode_band(pfc_rc_enc *e, pfc_ctx *w, const void *src,
                           uint32_t width, uint8_t bitdepth, uint32_t y0, uint32_t y1)
{
    uint32_t mid = (uint32_t)1u << (bitdepth - 1u);
    unsigned prev_k = 0u;
    uint32_t x, y;

    pfc_model_reset(w);
    for (y = y0; y < y1; y++) {
        for (x = 0u; x < width; x++) {
            uint32_t pred = med_predict(src, bitdepth, width, x, y, y0, mid);
            int32_t v = (int32_t)px_get(src, bitdepth, (size_t)y * width + x);
            pfc_resid_encode(e, w, &prev_k, v - (int32_t)pred);
        }
    }
    pfc_rc_enc_flush(e);
}

void pfc_image_decode_band(pfc_rc_dec *d, pfc_ctx *w, void *dst,
                           uint32_t width, uint8_t bitdepth, uint32_t y0, uint32_t y1)
{
    uint32_t mid = (uint32_t)1u << (bitdepth - 1u);
    unsigned prev_k = 0u;
    uint32_t x, y;

    pfc_model_reset(w);
    for (y = y0; y < y1; y++) {
        for (x = 0u; x < width; x++) {
            uint32_t pred = med_predict(dst, bitdepth, width, x, y, y0, mid);
            int32_t resid = pfc_resid_decode(d, w, &prev_k);
            px_set(dst, bitdepth, (size_t)y * width + x, (uint32_t)((int32_t)pred + resid));
        }
    }
}

/* Store-raw: canonical little-endian samples (endianness-neutral fallback). */
void pfc_image_store_raw(uint8_t *out, const void *src, uint32_t width, uint8_t bitdepth,
                         uint32_t y0, uint32_t y1)
{
    size_t n = (size_t)(y1 - y0) * width;
    size_t i;
    size_t base = (size_t)y0 * width;
    if (bitdepth > 8u) {
        for (i = 0u; i < n; i++) {
            uint32_t v = px_get(src, bitdepth, base + i);
            out[2u * i] = (uint8_t)(v & 0xFFu);
            out[2u * i + 1u] = (uint8_t)((v >> 8) & 0xFFu);
        }
    } else {
        for (i = 0u; i < n; i++) {
            out[i] = (uint8_t)px_get(src, bitdepth, base + i);
        }
    }
}

void pfc_image_load_raw(const uint8_t *in, void *dst, uint32_t width, uint8_t bitdepth,
                        uint32_t y0, uint32_t y1)
{
    size_t n = (size_t)(y1 - y0) * width;
    size_t i;
    size_t base = (size_t)y0 * width;
    if (bitdepth > 8u) {
        for (i = 0u; i < n; i++) {
            uint32_t v = (uint32_t)in[2u * i] | ((uint32_t)in[2u * i + 1u] << 8);
            px_set(dst, bitdepth, base + i, v);
        }
    } else {
        for (i = 0u; i < n; i++) {
            px_set(dst, bitdepth, base + i, (uint32_t)in[i]);
        }
    }
}
