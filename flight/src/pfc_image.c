/* pfc_image.c — 2-D MED (LOCO-I/JPEG-LS) predictor codec.
 * SPDX-License-Identifier: Apache-2.0
 *
 * The plane is coded in horizontal bands; each band predicts only from samples within the band, so
 * bands are independently decodable (R6). MED prediction is refined by LOCO-I-style per-context
 * bias correction, and residuals are entropy-coded with TWO decoupled contexts: a fine magnitude
 * context (carries the compression) and a coarse directional context (drives bias only). This
 * closed most of the gap to JPEG-LS. Integer-only, deterministic.
 *
 * Stream header (20 bytes): 'P''F''C''1' | ver | codec | bitdepth | rsvd | width | height | band.
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

static int iabs_i(int v)
{
    return (v < 0) ? -v : v;
}

/* MED predictor over neighbours available within band rows [y0,y1). */
static uint32_t med_predict(const void *p, uint8_t bd, uint32_t width,
                            uint32_t x, uint32_t y, uint32_t y0, uint32_t mid)
{
    uint32_t a, b, c, lo, hi;
    if ((y == y0) && (x == 0u)) {
        return mid;
    }
    if (y == y0) {
        return px_get(p, bd, (size_t)y * width + (x - 1u));
    }
    if (x == 0u) {
        return px_get(p, bd, (size_t)(y - 1u) * width + x);
    }
    a = px_get(p, bd, (size_t)y * width + (x - 1u));
    b = px_get(p, bd, (size_t)(y - 1u) * width + x);
    c = px_get(p, bd, (size_t)(y - 1u) * width + (x - 1u));
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

/* LOCO-I context + bias correction (the JPEG-LS levers our scalar context lacked). */
#define PFC_BIAS_RESET 64
#define PFC_C_MIN (-128)
#define PFC_C_MAX 127

/* Quantise a gradient into a signed level. 3 levels {-1,0,1} keeps the context count low (14) so
 * each per-band-reset context adapts well, while still giving bias correction a directional signal. */
static int quantize_grad(int dlt, int t1, int t2)
{
    int a = iabs_i(dlt);
    int lvl = (a < t1) ? 0 : 1;
    (void)t2;
    return (dlt < 0) ? -lvl : lvl;
}

static void grad_thresholds(uint8_t bd, int *t1, int *t2)
{
    if (bd > 8u) { *t1 = 32; *t2 = 64; } else { *t1 = 3; *t2 = 16; }
}

static unsigned bl32(uint32_t u)
{
    unsigned k = 0u;
    while (u != 0u) { k++; u >>= 1; }
    return k;
}

/* Two decoupled contexts from causal neighbours a(left) b(up) c(up-left) d(up-right):
 *  - emc: a FINE magnitude context (bit-length of |a-c|+|b-c|) for the entropy model — this
 *    carries most of the compression and adapts well even with per-band resets.
 *  - bq + sign: a COARSE directional context (3 sign-folded gradients -> 14) used ONLY for bias
 *    correction, which needs a signed/directional signal but little magnitude resolution. */
static void image_ctx(const void *p, uint8_t bd, uint32_t width,
                      uint32_t x, uint32_t y, uint32_t y0, int t1,
                      unsigned *emc, unsigned *bq, int *sign)
{
    int a, b, c, d, q1, q2, q3, idx;
    uint32_t g;
    if ((y == y0) || (x == 0u)) { *sign = 1; *bq = 0u; *emc = 0u; return; }
    a = (int)px_get(p, bd, (size_t)y * width + (x - 1u));
    b = (int)px_get(p, bd, (size_t)(y - 1u) * width + x);
    c = (int)px_get(p, bd, (size_t)(y - 1u) * width + (x - 1u));
    d = ((x + 1u) < width) ? (int)px_get(p, bd, (size_t)(y - 1u) * width + (x + 1u)) : b;
    g = (uint32_t)(iabs_i(a - c) + iabs_i(b - c));
    *emc = bl32(g);
    if (*emc >= PFC_NCTX) { *emc = PFC_NCTX - 1u; }
    q1 = quantize_grad(d - b, t1, 0);
    q2 = quantize_grad(b - c, t1, 0);
    q3 = quantize_grad(c - a, t1, 0);
    if ((q1 < 0) || ((q1 == 0) && (q2 < 0)) || ((q1 == 0) && (q2 == 0) && (q3 < 0))) {
        *sign = -1; q1 = -q1; q2 = -q2; q3 = -q3;
    } else {
        *sign = 1;
    }
    idx = ((q1 + 1) * 3 + (q2 + 1)) * 3 + (q3 + 1);   /* folded -> [13,26] */
    *bq = (unsigned)(idx - 13);                        /* -> [0,13] */
}

static void bias_reset(pfc_ctx *w)
{
    unsigned q;
    for (q = 0u; q < PFC_NCTX; q++) { w->bias_c[q] = 0; w->bias_b[q] = 0; w->bias_n[q] = 1; }
}

/* Update the per-context bias estimate (LOCO-I), using the sign-folded error. */
static void bias_update(pfc_ctx *w, unsigned q, int err)
{
    w->bias_b[q] += err;
    if (w->bias_n[q] >= PFC_BIAS_RESET) { w->bias_b[q] /= 2; w->bias_n[q] /= 2; }
    w->bias_n[q] += 1;
    if (w->bias_b[q] <= -w->bias_n[q]) {
        if (w->bias_c[q] > PFC_C_MIN) { w->bias_c[q] = (int16_t)(w->bias_c[q] - 1); }
        w->bias_b[q] += w->bias_n[q];
        if (w->bias_b[q] <= -w->bias_n[q]) { w->bias_b[q] = -w->bias_n[q] + 1; }
    } else if (w->bias_b[q] > 0) {
        if (w->bias_c[q] < PFC_C_MAX) { w->bias_c[q] = (int16_t)(w->bias_c[q] + 1); }
        w->bias_b[q] -= w->bias_n[q];
        if (w->bias_b[q] > 0) { w->bias_b[q] = 0; }
    } else {
        /* bias within tolerance — no change */
    }
}

void pfc_image_encode_band(pfc_rc_enc *e, pfc_ctx *w, const void *src,
                           uint32_t width, uint8_t bitdepth, uint32_t y0, uint32_t y1)
{
    uint32_t mid = (uint32_t)1u << (bitdepth - 1u);
    int maxval = (int)((1u << bitdepth) - 1u);
    int t1, t2;
    uint32_t x, y;
    grad_thresholds(bitdepth, &t1, &t2);
    pfc_model_reset(w);
    bias_reset(w);
    (void)t2;
    for (y = y0; y < y1; y++) {
        for (x = 0u; x < width; x++) {
            unsigned emc, bq; int sign;
            int px, ix, err;
            image_ctx(src, bitdepth, width, x, y, y0, t1, &emc, &bq, &sign);
            px = (int)med_predict(src, bitdepth, width, x, y, y0, mid);
            ix = (int)px_get(src, bitdepth, (size_t)y * width + x);
            px += sign * (int)w->bias_c[bq];
            if (px < 0) { px = 0; } else if (px > maxval) { px = maxval; }
            err = ix - px;
            if (sign < 0) { err = -err; }
            pfc_resid_encode(e, w, emc, err);
            bias_update(w, bq, err);
        }
    }
    pfc_rc_enc_flush(e);
}

void pfc_image_decode_band(pfc_rc_dec *d, pfc_ctx *w, void *dst,
                           uint32_t width, uint8_t bitdepth, uint32_t y0, uint32_t y1)
{
    uint32_t mid = (uint32_t)1u << (bitdepth - 1u);
    int maxval = (int)((1u << bitdepth) - 1u);
    int t1, t2;
    uint32_t x, y;
    grad_thresholds(bitdepth, &t1, &t2);
    pfc_model_reset(w);
    bias_reset(w);
    (void)t2;
    for (y = y0; y < y1; y++) {
        for (x = 0u; x < width; x++) {
            unsigned emc, bq; int sign;
            int px, errf, err, ix;
            image_ctx(dst, bitdepth, width, x, y, y0, t1, &emc, &bq, &sign);
            px = (int)med_predict(dst, bitdepth, width, x, y, y0, mid);
            px += sign * (int)w->bias_c[bq];
            if (px < 0) { px = 0; } else if (px > maxval) { px = maxval; }
            errf = pfc_resid_decode(d, w, emc);
            bias_update(w, bq, errf);
            err = (sign < 0) ? -errf : errf;
            ix = px + err;
            px_set(dst, bitdepth, (size_t)y * width + x, (uint32_t)ix);
        }
    }
}

void pfc_image_store_raw(uint8_t *out, const void *src, uint32_t width, uint8_t bitdepth,
                         uint32_t y0, uint32_t y1)
{
    size_t n = (size_t)(y1 - y0) * width;
    size_t base = (size_t)y0 * width;
    size_t i;
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
    size_t base = (size_t)y0 * width;
    size_t i;
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

/* ---------------------------------------------------------------- codec ---- */

pfc_status pfc_image_encode(const pfc_params *p, const void *src, uint8_t *dst, size_t cap,
                            size_t *pos, pfc_ctx *w)
{
    uint8_t es = (p->bitdepth > 8u) ? 2u : 1u;
    uint32_t band = PFC_BAND_ROWS;
    size_t at;
    uint32_t y0;

    if (((p->bitdepth != 8u) && (p->bitdepth != 16u)) ||
        (p->width == 0u) || (p->height == 0u) || (p->width > PFC_MAX_COLS)) {
        return PFC_E_PARAM;
    }
    if (cap < PFC_HDR) {
        return PFC_E_BOUND;
    }
    dst[0] = 'P'; dst[1] = 'F'; dst[2] = 'C'; dst[3] = '1';
    dst[4] = (uint8_t)PFC_VERSION;
    dst[5] = (uint8_t)PFC_CODEC_IMAGE;
    dst[6] = p->bitdepth;
    dst[7] = 0u;
    pfc_put_u32(&dst[8], p->width);
    pfc_put_u32(&dst[12], p->height);
    pfc_put_u32(&dst[16], band);
    at = PFC_HDR;

    for (y0 = 0u; y0 < p->height; y0 += band) {
        uint32_t y1 = y0 + band;
        size_t raw_bytes;
        size_t plen;
        uint8_t flags;
        pfc_rc_enc e;
        pfc_status st;

        if (y1 > p->height) {
            y1 = p->height;
        }
        raw_bytes = (size_t)(y1 - y0) * p->width * es;
        pfc_rc_enc_init(&e, w->scratch, raw_bytes);
        pfc_image_encode_band(&e, w, src, p->width, p->bitdepth, y0, y1);
        if ((e.overflow != 0) || (e.pos >= raw_bytes)) {
            pfc_image_store_raw(w->scratch, src, p->width, p->bitdepth, y0, y1);
            plen = raw_bytes;
            flags = PFC_BLK_FLAG_RAW;
        } else {
            plen = e.pos;
            flags = 0u;
        }
        st = pfc_block_write(dst, cap, &at, w->scratch, plen, flags);
        if (st != PFC_OK) {
            return st;
        }
    }
    *pos = at;
    return PFC_OK;
}

static void fill_band(void *dst, uint8_t bitdepth, uint32_t width, uint32_t y0, uint32_t y1)
{
    uint32_t mid = (uint32_t)1u << (bitdepth - 1u);
    size_t base = (size_t)y0 * width;
    size_t n = (size_t)(y1 - y0) * width;
    size_t i;
    for (i = 0u; i < n; i++) {
        px_set(dst, bitdepth, base + i, mid);
    }
}

pfc_status pfc_image_decode(const uint8_t *s, size_t len, void *dst, size_t cap,
                            size_t *out, pfc_ctx *w, int *corrupt)
{
    uint8_t bitdepth = s[6];
    uint32_t width = pfc_get_u32(&s[8]);
    uint32_t height = pfc_get_u32(&s[12]);
    uint32_t band = pfc_get_u32(&s[16]);
    uint8_t es;
    size_t pos = PFC_HDR;
    uint32_t y0;

    if ((bitdepth != 8u) && (bitdepth != 16u)) {
        return PFC_E_CORRUPT;
    }
    if ((width == 0u) || (height == 0u) || (band == 0u) || (width > PFC_MAX_COLS)) {
        return PFC_E_CORRUPT;
    }
    es = (bitdepth > 8u) ? 2u : 1u;
    if (cap < ((size_t)width * height * es)) {
        return PFC_E_BOUND;
    }

    for (y0 = 0u; y0 < height; y0 += band) {
        uint32_t y1 = y0 + band;
        const uint8_t *payload;
        size_t plen;
        uint8_t flags;
        size_t before = pos;
        pfc_status st;

        if (y1 > height) {
            y1 = height;
        }
        st = pfc_block_read(s, len, &pos, &payload, &plen, &flags);
        if (st != PFC_OK) {
            fill_band(dst, bitdepth, width, y0, y1);
            *corrupt = 1;
            if (pos == before) {
                /* truncation: fill all remaining bands, stop reading */
                for (y0 = y1; y0 < height; y0 += band) {
                    uint32_t yy = y0 + band;
                    if (yy > height) { yy = height; }
                    fill_band(dst, bitdepth, width, y0, yy);
                }
                break;
            }
            continue;
        }
        if ((flags & PFC_BLK_FLAG_RAW) != 0u) {
            size_t expect = (size_t)(y1 - y0) * width * es;
            if (plen != expect) {
                fill_band(dst, bitdepth, width, y0, y1);
                *corrupt = 1;
            } else {
                pfc_image_load_raw(payload, dst, width, bitdepth, y0, y1);
            }
        } else {
            pfc_rc_dec dec;
            pfc_rc_dec_init(&dec, payload, plen);
            pfc_image_decode_band(&dec, w, dst, width, bitdepth, y0, y1);
        }
    }
    *out = (size_t)width * height * es;
    return PFC_OK;
}
