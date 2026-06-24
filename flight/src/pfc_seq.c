/* pfc_seq.c — 1-D integer sequence codec (order-1 delta + range coder).
 * SPDX-License-Identifier: Apache-2.0
 *
 * For housekeeping / telemetry / time-series channels: count integers of elem bytes (1/2/4),
 * signed or unsigned. Each block (PFC_BLOCK_BYTES worth of samples) is delta-predicted from the
 * previous sample (first sample from 0), residual range-coded with a clamped-previous-category
 * context. Blocks are independent (R6). Wrapping 32-bit arithmetic -> exact for any elem.
 *
 * Header (20): 'P''F''C''1' | ver | codec | elem | is_signed | count(4) | block_samples(4) | rsvd(4).
 */
#include "pfc_internal.h"

static int32_t s_get(const void *src, uint8_t elem, uint8_t is_signed, size_t i)
{
    if (elem == 1u) {
        uint8_t v = ((const uint8_t *)src)[i];
        return is_signed ? (int32_t)(int8_t)v : (int32_t)v;
    }
    if (elem == 2u) {
        uint16_t v = ((const uint16_t *)src)[i];
        return is_signed ? (int32_t)(int16_t)v : (int32_t)v;
    }
    return (int32_t)((const uint32_t *)src)[i];
}

static void s_set(void *dst, uint8_t elem, size_t i, int32_t v)
{
    if (elem == 1u) {
        ((uint8_t *)dst)[i] = (uint8_t)v;
    } else if (elem == 2u) {
        ((uint16_t *)dst)[i] = (uint16_t)v;
    } else {
        ((uint32_t *)dst)[i] = (uint32_t)v;
    }
}

/* canonical little-endian raw (store-raw fallback) */
static void raw_store(uint8_t *out, const void *src, uint8_t elem, uint8_t is_signed,
                      size_t i0, size_t n)
{
    size_t i, j;
    for (i = 0u; i < n; i++) {
        uint32_t v = (uint32_t)s_get(src, elem, is_signed, i0 + i);
        for (j = 0u; j < elem; j++) {
            out[i * elem + j] = (uint8_t)((v >> (8u * j)) & 0xFFu);
        }
    }
}

static void raw_load(const uint8_t *in, void *dst, uint8_t elem, uint8_t is_signed,
                     size_t i0, size_t n)
{
    size_t i, j;
    for (i = 0u; i < n; i++) {
        uint32_t v = 0u;
        for (j = 0u; j < elem; j++) {
            v |= (uint32_t)in[i * elem + j] << (8u * j);
        }
        if (is_signed && (elem == 1u)) { v = (uint32_t)(int32_t)(int8_t)(uint8_t)v; }
        else if (is_signed && (elem == 2u)) { v = (uint32_t)(int32_t)(int16_t)(uint16_t)v; }
        s_set(dst, elem, i0 + i, (int32_t)v);
    }
}

pfc_status pfc_seq_encode(const pfc_params *p, const void *src, uint8_t *dst, size_t cap,
                          size_t *pos, pfc_ctx *w)
{
    uint32_t block = PFC_BLOCK_BYTES / p->elem;     /* samples per block */
    size_t at;
    size_t i0;

    if ((p->count == 0u) || (block == 0u)) {
        return PFC_E_PARAM;
    }
    if (cap < PFC_HDR) {
        return PFC_E_BOUND;
    }
    dst[0] = 'P'; dst[1] = 'F'; dst[2] = 'C'; dst[3] = '1';
    dst[4] = (uint8_t)PFC_VERSION;
    dst[5] = (uint8_t)PFC_CODEC_SEQ;
    dst[6] = p->elem;
    dst[7] = p->is_signed;
    pfc_put_u32(&dst[8], p->count);
    pfc_put_u32(&dst[12], block);
    pfc_put_u32(&dst[16], 0u);
    at = PFC_HDR;

    for (i0 = 0u; i0 < p->count; i0 += block) {
        size_t n = ((i0 + block) <= p->count) ? block : (p->count - i0);
        size_t raw_bytes = n * p->elem;
        size_t plen;
        uint8_t flags;
        pfc_rc_enc e;
        pfc_status st;
        uint32_t prev = 0u;
        unsigned ctx = 0u;
        size_t i;

        pfc_model_reset(w);
        pfc_rc_enc_init(&e, w->scratch, raw_bytes);
        for (i = 0u; i < n; i++) {
            uint32_t cur = (uint32_t)s_get(src, p->elem, p->is_signed, i0 + i);
            int32_t resid = (int32_t)(cur - prev);
            pfc_resid_encode(&e, w, ctx, resid);
            ctx = pfc_cat(resid);
            prev = cur;
        }
        pfc_rc_enc_flush(&e);

        if ((e.overflow != 0) || (e.pos >= raw_bytes)) {
            raw_store(w->scratch, src, p->elem, p->is_signed, i0, n);
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

pfc_status pfc_seq_decode(const uint8_t *s, size_t len, void *dst, size_t cap,
                          size_t *out, pfc_ctx *w, int *corrupt)
{
    uint8_t elem = s[6];
    uint8_t is_signed = s[7];
    uint32_t count = pfc_get_u32(&s[8]);
    uint32_t block = pfc_get_u32(&s[12]);
    size_t pos = PFC_HDR;
    size_t i0;

    if (((elem != 1u) && (elem != 2u) && (elem != 4u)) || (count == 0u) || (block == 0u)) {
        return PFC_E_CORRUPT;
    }
    if (cap < ((size_t)count * elem)) {
        return PFC_E_BOUND;
    }

    for (i0 = 0u; i0 < count; i0 += block) {
        size_t n = ((i0 + block) <= count) ? block : (count - i0);
        const uint8_t *payload;
        size_t plen;
        uint8_t flags;
        size_t before = pos;
        pfc_status st = pfc_block_read(s, len, &pos, &payload, &plen, &flags);
        size_t i;

        if (st != PFC_OK) {
            *corrupt = 1;
            if (pos == before) {            /* truncation: zero this + all remaining, stop */
                size_t j;
                for (j = i0; j < count; j++) { s_set(dst, elem, j, 0); }
                break;
            }
            for (i = 0u; i < n; i++) { s_set(dst, elem, i0 + i, 0); }
            continue;
        }
        if ((flags & PFC_BLK_FLAG_RAW) != 0u) {
            if (plen != (n * elem)) {
                for (i = 0u; i < n; i++) { s_set(dst, elem, i0 + i, 0); }
                *corrupt = 1;
            } else {
                raw_load(payload, dst, elem, is_signed, i0, n);
            }
        } else {
            pfc_rc_dec dec;
            uint32_t prev = 0u;
            unsigned ctx = 0u;
            pfc_model_reset(w);
            pfc_rc_dec_init(&dec, payload, plen);
            for (i = 0u; i < n; i++) {
                int32_t resid = pfc_resid_decode(&dec, w, ctx);
                uint32_t cur = prev + (uint32_t)resid;
                s_set(dst, elem, i0 + i, (int32_t)cur);
                ctx = pfc_cat(resid);
                prev = cur;
            }
        }
    }
    /* zero any tail bands left unwritten by an early truncation break */
    *out = (size_t)count * elem;
    return PFC_OK;
}
