/* pfc_columnar.c — byte-plane de-interleave codec; also backs PFC_CODEC_FLOAT.
 * SPDX-License-Identifier: Apache-2.0
 *
 * Input is `count` records of `rec_width` bytes (row-major). Each block de-interleaves its records
 * into rec_width byte-planes; each plane is range-coded (optionally order-1 delta'd) with a
 * clamped-previous-category context. Blocks are independent (R6); a losing block stores raw.
 *  - COLUMNAR (do_delta=1): rec_width = params.width, records = params.count.
 *  - FLOAT    (do_delta=0): rec_width = params.elem (4/8), records = params.count (byte-plane split).
 *
 * Header (20): 'P''F''C''1' | ver | codec | do_delta | rsvd | rec_width(4) | count(4) | block_recs(4).
 */
#include "pfc_internal.h"

pfc_status pfc_columnar_encode(uint8_t codec, const pfc_params *p, const void *src,
                               uint8_t *dst, size_t cap, size_t *pos, pfc_ctx *w)
{
    const uint8_t *sb = (const uint8_t *)src;
    uint32_t rw = (codec == (uint8_t)PFC_CODEC_FLOAT) ? p->elem : p->width;
    uint32_t cnt = p->count;
    uint8_t do_delta = (uint8_t)((codec == (uint8_t)PFC_CODEC_FLOAT) ? 0 : 1);
    uint32_t block_recs;
    size_t at;
    size_t r0;

    if ((rw == 0u) || (rw > PFC_BLOCK_BYTES) || (cnt == 0u)) {
        return PFC_E_PARAM;
    }
    block_recs = PFC_BLOCK_BYTES / rw;
    if (cap < PFC_HDR) {
        return PFC_E_BOUND;
    }
    dst[0] = 'P'; dst[1] = 'F'; dst[2] = 'C'; dst[3] = '1';
    dst[4] = (uint8_t)PFC_VERSION;
    dst[5] = codec;
    dst[6] = do_delta;
    dst[7] = 0u;
    pfc_put_u32(&dst[8], rw);
    pfc_put_u32(&dst[12], cnt);
    pfc_put_u32(&dst[16], block_recs);
    at = PFC_HDR;

    for (r0 = 0u; r0 < cnt; r0 += block_recs) {
        size_t nr = ((r0 + block_recs) <= cnt) ? block_recs : (cnt - r0);
        size_t block_bytes = nr * rw;
        size_t plen;
        uint8_t flags;
        pfc_rc_enc e;
        pfc_status st;
        uint32_t c;
        size_t r;

        /* de-interleave records -> planes in xform[c*nr + r] */
        for (c = 0u; c < rw; c++) {
            for (r = 0u; r < nr; r++) {
                w->xform[(size_t)c * nr + r] = sb[(r0 + r) * rw + c];
            }
        }
        pfc_rc_enc_init(&e, w->scratch, block_bytes);
        for (c = 0u; c < rw; c++) {
            uint32_t prev = 0u;
            unsigned ctx = 0u;
            pfc_model_reset(w);
            for (r = 0u; r < nr; r++) {
                uint32_t cur = w->xform[(size_t)c * nr + r];
                int32_t resid = do_delta ? (int32_t)(cur - prev) : (int32_t)cur;
                pfc_resid_encode(&e, w, ctx, resid);
                ctx = pfc_cat(resid);
                prev = cur;
            }
        }
        pfc_rc_enc_flush(&e);

        if ((e.overflow != 0) || (e.pos >= block_bytes)) {
            for (r = 0u; r < block_bytes; r++) {
                w->scratch[r] = sb[r0 * rw + r];
            }
            plen = block_bytes;
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

pfc_status pfc_columnar_decode(const uint8_t *s, size_t len, void *dst, size_t cap,
                               size_t *out, pfc_ctx *w, int *corrupt)
{
    uint8_t *db = (uint8_t *)dst;
    uint8_t do_delta = s[6];
    uint32_t rw = pfc_get_u32(&s[8]);
    uint32_t cnt = pfc_get_u32(&s[12]);
    uint32_t block_recs = pfc_get_u32(&s[16]);
    size_t pos = PFC_HDR;
    size_t r0;

    if ((rw == 0u) || (rw > PFC_BLOCK_BYTES) || (cnt == 0u) || (block_recs == 0u)) {
        return PFC_E_CORRUPT;
    }
    if (cap < ((size_t)rw * cnt)) {
        return PFC_E_BOUND;
    }

    for (r0 = 0u; r0 < cnt; r0 += block_recs) {
        size_t nr = ((r0 + block_recs) <= cnt) ? block_recs : (cnt - r0);
        size_t block_bytes = nr * rw;
        const uint8_t *payload;
        size_t plen;
        uint8_t flags;
        size_t before = pos;
        pfc_status st;
        uint32_t c;
        size_t r;

        /* block_recs comes straight from the untrusted stream header (unlike the encoder, which
         * always derives it safely from rw so rw*block_recs <= PFC_BLOCK_BYTES) -- a corrupted or
         * malicious stream can set it arbitrarily large. The range-coded branch below writes
         * block_bytes bytes into w->xform, a fixed PFC_BLOCK_BYTES-byte scratch buffer; without
         * this check that write overflows it. Found by libFuzzer (heap-buffer-overflow,
         * pfc_columnar.c:150, UBSan: index 65536 out of bounds for xform[65536]). */
        if (block_bytes > PFC_BLOCK_BYTES) { return PFC_E_CORRUPT; }

        st = pfc_block_read(s, len, &pos, &payload, &plen, &flags);
        if (st != PFC_OK) {
            *corrupt = 1;
            if (pos == before) {                       /* truncation: zero remainder, stop */
                for (r = (size_t)r0 * rw; r < ((size_t)cnt * rw); r++) { db[r] = 0u; }
                break;
            }
            for (r = 0u; r < block_bytes; r++) { db[r0 * rw + r] = 0u; }
            continue;
        }
        if ((flags & PFC_BLK_FLAG_RAW) != 0u) {
            if (plen != block_bytes) {
                for (r = 0u; r < block_bytes; r++) { db[r0 * rw + r] = 0u; }
                *corrupt = 1;
            } else {
                for (r = 0u; r < block_bytes; r++) { db[r0 * rw + r] = payload[r]; }
            }
            continue;
        }
        {
            pfc_rc_dec dec;
            pfc_rc_dec_init(&dec, payload, plen);
            for (c = 0u; c < rw; c++) {
                uint32_t prev = 0u;
                unsigned ctx = 0u;
                pfc_model_reset(w);
                for (r = 0u; r < nr; r++) {
                    int32_t resid = pfc_resid_decode(&dec, w, ctx);
                    uint32_t cur = do_delta ? ((prev + (uint32_t)resid) & 0xFFu)
                                            : ((uint32_t)resid & 0xFFu);
                    w->xform[(size_t)c * nr + r] = (uint8_t)cur;
                    ctx = pfc_cat(resid);
                    prev = cur;
                }
            }
            for (c = 0u; c < rw; c++) {
                for (r = 0u; r < nr; r++) {
                    db[(r0 + r) * rw + c] = w->xform[(size_t)c * nr + r];
                }
            }
        }
    }
    *out = (size_t)rw * cnt;
    return PFC_OK;
}
