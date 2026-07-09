/* pfc_spectral.c — spectral+spatial codec for multi/hyperspectral cubes (CCSDS-123 territory).
 * SPDX-License-Identifier: Apache-2.0
 *
 * Input is a band-sequential (BSQ) cube: Z bands, each H x W samples (8- or 16-bit), contiguous.
 * Each sample is predicted as  s_hat = s(z-1,y,x) + MED(dW, dN, dNW)  where d() are the inter-band
 * differences s(z,*) - s(z-1,*) of the causal spatial neighbours — i.e. MED prediction applied to
 * the inter-band difference image. This captures spectral correlation AND the gain-induced spatial
 * structure that defeats a naive band delta. Band 0 uses s(z-1)=0, so it reduces to plain spatial
 * MED. Residuals go through the shared arithmetic + mantissa-bit coder.
 *
 * Framing: per (band, row-block) so a block's coded payload fits the scratch buffer (W <= MAX_COLS).
 * Each block is CRC-protected; spectral prediction reads the previously-decoded band, so on a
 * corrupted block error is contained spatially to its rows but can propagate along the band axis
 * (the inherent cost of inter-band prediction; a deployment can insert periodic spatial refresh).
 *
 * Header (24 bytes): 'P''F''C''1' | ver | codec | bitdepth | rsvd | W | H | Z | band_rows.
 */
#include "pfc_internal.h"

#define PFC_SPEC_HDR 24u

static uint32_t sx_get(const void *p, uint8_t bd, size_t i)
{
    return (bd > 8u) ? (uint32_t)((const uint16_t *)p)[i] : (uint32_t)((const uint8_t *)p)[i];
}

static void sx_set(void *p, uint8_t bd, size_t i, uint32_t v)
{
    if (bd > 8u) { ((uint16_t *)p)[i] = (uint16_t)v; } else { ((uint8_t *)p)[i] = (uint8_t)v; }
}

static int si_med(int a, int b, int c)
{
    int lo = (a < b) ? a : b;
    int hi = (a > b) ? a : b;
    if (c >= hi) { return lo; }
    if (c <= lo) { return hi; }
    return a + b - c;
}

static int si_abs(int v) { return (v < 0) ? -v : v; }

static unsigned si_bitlen(uint32_t u)
{
    unsigned k = 0u;
    while (u != 0u) { k++; u >>= 1; }
    return k;
}

/* Inter-band difference at (y,x): s(z,y,x) - s(z-1,y,x), reading from `p` (src on encode/dst on
 * decode). For z==0 the previous band is treated as 0, so diff == sample (=> spatial MED). */
static int spec_diff(const void *p, uint8_t bd, size_t bz, size_t bzp, int has_prev,
                     uint32_t width, uint32_t y, uint32_t x)
{
    int s = (int)sx_get(p, bd, bz + (size_t)y * width + x);
    int sp = has_prev ? (int)sx_get(p, bd, bzp + (size_t)y * width + x) : 0;
    return s - sp;
}

/* Predict the difference at (y,x) from causal neighbours; also yield the entropy context. */
static int spec_predict(const void *p, uint8_t bd, size_t bz, size_t bzp, int has_prev,
                        uint32_t width, uint32_t y, uint32_t x, uint32_t y0, unsigned *ctx)
{
    int haveW = (x > 0u);
    int haveN = (y > y0);
    int dW = 0, dN = 0;
    if (haveW) { dW = spec_diff(p, bd, bz, bzp, has_prev, width, y, x - 1u); }
    if (haveN) { dN = spec_diff(p, bd, bz, bzp, has_prev, width, y - 1u, x); }

    if (haveW && haveN) {
        uint32_t g;
        unsigned c;
        int dNW = spec_diff(p, bd, bz, bzp, has_prev, width, y - 1u, x - 1u);
        g = (uint32_t)(si_abs(dW - dNW) + si_abs(dN - dNW));
        c = si_bitlen(g);
        *ctx = (c < PFC_NCTX) ? c : (PFC_NCTX - 1u);
        return si_med(dW, dN, dNW);
    }
    *ctx = 0u;
    if (!haveW && !haveN) { return 0; }    /* block top-left: predict prev-band value (diff 0) */
    return haveN ? dN : dW;
}

static void spec_encode_block(pfc_rc_enc *e, pfc_ctx *w, const void *src, uint8_t bd,
                              uint32_t width, uint32_t height, uint32_t z, uint32_t y0, uint32_t y1)
{
    size_t bz = (size_t)z * height * width;
    size_t bzp = (z > 0u) ? (size_t)(z - 1u) * height * width : 0u;
    int has_prev = (z > 0u);
    uint32_t x, y;
    pfc_model_reset(w);
    for (y = y0; y < y1; y++) {
        for (x = 0u; x < width; x++) {
            unsigned ctx;
            int pred = spec_predict(src, bd, bz, bzp, has_prev, width, y, x, y0, &ctx);
            int diff = spec_diff(src, bd, bz, bzp, has_prev, width, y, x);
            pfc_resid_encode(e, w, ctx, diff - pred);
        }
    }
    pfc_rc_enc_flush(e);
}

static void spec_decode_block(pfc_rc_dec *d, pfc_ctx *w, void *dst, uint8_t bd,
                              uint32_t width, uint32_t height, uint32_t z, uint32_t y0, uint32_t y1)
{
    size_t bz = (size_t)z * height * width;
    size_t bzp = (z > 0u) ? (size_t)(z - 1u) * height * width : 0u;
    int has_prev = (z > 0u);
    uint32_t x, y;
    pfc_model_reset(w);
    for (y = y0; y < y1; y++) {
        for (x = 0u; x < width; x++) {
            unsigned ctx;
            int pred = spec_predict(dst, bd, bz, bzp, has_prev, width, y, x, y0, &ctx);
            int diff = pred + pfc_resid_decode(d, w, ctx);
            int sp = has_prev ? (int)sx_get(dst, bd, bzp + (size_t)y * width + x) : 0;
            sx_set(dst, bd, bz + (size_t)y * width + x, (uint32_t)(sp + diff));
        }
    }
}

static void spec_fill_block(void *dst, uint8_t bd, uint32_t width, uint32_t height,
                            uint32_t z, uint32_t y0, uint32_t y1)
{
    uint32_t mid = (uint32_t)1u << (bd - 1u);
    size_t base = (size_t)z * height * width + (size_t)y0 * width;
    size_t n = (size_t)(y1 - y0) * width;
    size_t i;
    for (i = 0u; i < n; i++) { sx_set(dst, bd, base + i, mid); }
}

static void spec_store_raw(uint8_t *out, const void *src, uint8_t bd, uint32_t width, uint32_t height,
                           uint32_t z, uint32_t y0, uint32_t y1)
{
    size_t base = (size_t)z * height * width + (size_t)y0 * width;
    size_t n = (size_t)(y1 - y0) * width;
    size_t i;
    if (bd > 8u) {
        for (i = 0u; i < n; i++) {
            uint32_t v = sx_get(src, bd, base + i);
            out[2u * i] = (uint8_t)(v & 0xFFu);
            out[2u * i + 1u] = (uint8_t)((v >> 8) & 0xFFu);
        }
    } else {
        for (i = 0u; i < n; i++) { out[i] = (uint8_t)sx_get(src, bd, base + i); }
    }
}

static void spec_load_raw(const uint8_t *in, void *dst, uint8_t bd, uint32_t width, uint32_t height,
                          uint32_t z, uint32_t y0, uint32_t y1)
{
    size_t base = (size_t)z * height * width + (size_t)y0 * width;
    size_t n = (size_t)(y1 - y0) * width;
    size_t i;
    if (bd > 8u) {
        for (i = 0u; i < n; i++) {
            sx_set(dst, bd, base + i, (uint32_t)in[2u * i] | ((uint32_t)in[2u * i + 1u] << 8));
        }
    } else {
        for (i = 0u; i < n; i++) { sx_set(dst, bd, base + i, (uint32_t)in[i]); }
    }
}

pfc_status pfc_spectral_encode(const pfc_params *p, const void *src, uint8_t *dst, size_t cap,
                               size_t *pos, pfc_ctx *w)
{
    uint8_t es = (p->bitdepth > 8u) ? 2u : 1u;
    uint32_t band = PFC_BAND_ROWS;
    size_t at;
    uint32_t z, y0;

    if (((p->bitdepth != 8u) && (p->bitdepth != 16u)) ||
        (p->width == 0u) || (p->height == 0u) || (p->count == 0u) || (p->width > PFC_MAX_COLS)) {
        return PFC_E_PARAM;
    }
    if (cap < PFC_SPEC_HDR) { return PFC_E_BOUND; }
    dst[0] = 'P'; dst[1] = 'F'; dst[2] = 'C'; dst[3] = '1';
    dst[4] = (uint8_t)PFC_VERSION;
    dst[5] = (uint8_t)PFC_CODEC_SPECTRAL;
    dst[6] = p->bitdepth;
    dst[7] = 0u;
    pfc_put_u32(&dst[8], p->width);
    pfc_put_u32(&dst[12], p->height);
    pfc_put_u32(&dst[16], p->count);
    pfc_put_u32(&dst[20], band);
    at = PFC_SPEC_HDR;

    for (z = 0u; z < p->count; z++) {
        for (y0 = 0u; y0 < p->height; y0 += band) {
            uint32_t y1 = y0 + band;
            size_t raw_bytes;
            size_t plen;
            uint8_t flags;
            pfc_rc_enc e;
            pfc_status st;
            if (y1 > p->height) { y1 = p->height; }
            raw_bytes = (size_t)(y1 - y0) * p->width * es;
            pfc_rc_enc_init(&e, w->scratch, raw_bytes);
            spec_encode_block(&e, w, src, p->bitdepth, p->width, p->height, z, y0, y1);
            if ((e.overflow != 0) || (e.pos >= raw_bytes)) {
                spec_store_raw(w->scratch, src, p->bitdepth, p->width, p->height, z, y0, y1);
                plen = raw_bytes; flags = PFC_BLK_FLAG_RAW;
            } else {
                plen = e.pos; flags = 0u;
            }
            st = pfc_block_write(dst, cap, &at, w->scratch, plen, flags);
            if (st != PFC_OK) { return st; }
        }
    }
    *pos = at;
    return PFC_OK;
}

pfc_status pfc_spectral_decode(const uint8_t *s, size_t len, void *dst, size_t cap,
                               size_t *out, pfc_ctx *w, int *corrupt)
{
    uint8_t bd, es;
    uint32_t width, height, count, band, z, y0;
    size_t pos;

    /* SPECTRAL's header is PFC_SPEC_HDR (24) bytes, larger than the generic PFC_HDR (20) the
     * top-level pfc_decode() dispatcher checks before routing here — that check alone is NOT
     * sufficient for this codec (found via libFuzzer: a 20-23 byte crafted input reads past the
     * buffer at s[20..23] below). Every other codec's header is exactly PFC_HDR, so they can rely
     * on the dispatcher's check alone; this one cannot. */
    if (len < PFC_SPEC_HDR) { return PFC_E_CORRUPT; }

    bd = s[6];
    width = pfc_get_u32(&s[8]);
    height = pfc_get_u32(&s[12]);
    count = pfc_get_u32(&s[16]);
    band = pfc_get_u32(&s[20]);
    pos = PFC_SPEC_HDR;

    if ((bd != 8u) && (bd != 16u)) { return PFC_E_CORRUPT; }
    if ((width == 0u) || (height == 0u) || (count == 0u) || (band == 0u) || (width > PFC_MAX_COLS)) {
        return PFC_E_CORRUPT;
    }
    es = (bd > 8u) ? 2u : 1u;
    if (cap < ((size_t)width * height * count * es)) { return PFC_E_BOUND; }

    for (z = 0u; z < count; z++) {
        for (y0 = 0u; y0 < height; y0 += band) {
            uint32_t y1 = y0 + band;
            const uint8_t *payload;
            size_t plen;
            uint8_t flags;
            size_t before = pos;
            pfc_status st;
            if (y1 > height) { y1 = height; }
            st = pfc_block_read(s, len, &pos, &payload, &plen, &flags);
            if (st != PFC_OK) {
                spec_fill_block(dst, bd, width, height, z, y0, y1);
                *corrupt = 1;
                if (pos == before) {        /* truncation: fill the rest of the cube, stop */
                    uint32_t zz, yy;
                    for (zz = z; zz < count; zz++) {
                        for (yy = (zz == z) ? y1 : 0u; yy < height; yy += band) {
                            uint32_t yy1 = yy + band;
                            if (yy1 > height) { yy1 = height; }
                            spec_fill_block(dst, bd, width, height, zz, yy, yy1);
                        }
                    }
                    *out = (size_t)width * height * count * es;
                    return PFC_OK;
                }
                continue;
            }
            if ((flags & PFC_BLK_FLAG_RAW) != 0u) {
                size_t expect = (size_t)(y1 - y0) * width * es;
                if (plen != expect) {
                    spec_fill_block(dst, bd, width, height, z, y0, y1);
                    *corrupt = 1;
                } else {
                    spec_load_raw(payload, dst, bd, width, height, z, y0, y1);
                }
            } else {
                pfc_rc_dec dec;
                pfc_rc_dec_init(&dec, payload, plen);
                spec_decode_block(&dec, w, dst, bd, width, height, z, y0, y1);
            }
        }
    }
    *out = (size_t)width * height * count * es;
    return PFC_OK;
}
