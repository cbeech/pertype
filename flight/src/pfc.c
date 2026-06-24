/* pfc.c — top-level framing, dispatch, and the public API.
 * SPDX-License-Identifier: Apache-2.0
 *
 * Stream layout (canonical little-endian, R4):
 *   header[20] = 'P''F''C''1' | ver(1) | codec(1) | bitdepth(1) | rsvd(1)
 *                | width(4) | height(4) | band_rows(4)
 *   then n_blocks = ceil(height/band_rows) records, each:
 *     payload_len(4) | flags(1) | crc32(4) | payload[payload_len]
 *   flags.bit0 = store-raw (no-expansion fallback, R5). Each block is independently
 *   decodable (R6): a CRC mismatch is repaired in place and reported, never propagated.
 */
#include "pfc_internal.h"

#define PFC_HDR    20u
#define PFC_BLKHDR 9u

size_t pfc_workmem_bytes(void)
{
    return sizeof(struct pfc_ctx);
}

size_t pfc_bound(pfc_codec codec, size_t n_in)
{
    (void)codec;
    /* header + worst-case store-raw payload + generous per-band overhead headroom. */
    return n_in + (n_in / 2u) + 1024u;
}

static uint8_t elem_size(uint8_t bitdepth)
{
    return (bitdepth > 8u) ? 2u : 1u;
}

/* ------------------------------------------------------------------ encode ---- */

pfc_status pfc_encode(pfc_codec codec, const pfc_params *p,
                      const void *src, size_t src_len,
                      void *dst, size_t cap, size_t *out, pfc_ctx *work)
{
    uint8_t *d = (uint8_t *)dst;
    uint32_t band_rows = PFC_BAND_ROWS;
    uint8_t es;
    size_t need_in;
    size_t pos;
    uint32_t y0;

    if ((p == NULL) || (src == NULL) || (dst == NULL) || (out == NULL) || (work == NULL)) {
        return PFC_E_PARAM;
    }
    if (codec != PFC_CODEC_IMAGE) {
        return PFC_E_UNSUPPORTED;
    }
    if (((p->bitdepth != 8u) && (p->bitdepth != 16u)) ||
        (p->width == 0u) || (p->height == 0u) || (p->width > PFC_MAX_COLS)) {
        return PFC_E_PARAM;
    }
    es = elem_size(p->bitdepth);
    need_in = (size_t)p->width * p->height * es;
    if (src_len != need_in) {
        return PFC_E_PARAM;
    }
    if (cap < PFC_HDR) {
        return PFC_E_BOUND;
    }

    d[0] = 'P'; d[1] = 'F'; d[2] = 'C'; d[3] = '1';
    d[4] = (uint8_t)PFC_VERSION;
    d[5] = (uint8_t)codec;
    d[6] = p->bitdepth;
    d[7] = 0u;
    pfc_put_u32(&d[8], p->width);
    pfc_put_u32(&d[12], p->height);
    pfc_put_u32(&d[16], band_rows);
    pos = PFC_HDR;

    for (y0 = 0u; y0 < p->height; y0 += band_rows) {
        uint32_t y1 = y0 + band_rows;
        size_t raw_bytes;
        size_t plen;
        uint8_t flags;
        uint32_t crc;
        pfc_rc_enc e;

        if (y1 > p->height) {
            y1 = p->height;
        }
        raw_bytes = (size_t)(y1 - y0) * p->width * es;

        /* Try range coding into scratch, capped at the raw size so a losing band overflows. */
        pfc_rc_enc_init(&e, work->scratch, raw_bytes);
        pfc_image_encode_band(&e, work, src, p->width, p->bitdepth, y0, y1);

        if ((e.overflow != 0) || (e.pos >= raw_bytes)) {
            pfc_image_store_raw(work->scratch, src, p->width, p->bitdepth, y0, y1);
            plen = raw_bytes;
            flags = 1u;
        } else {
            plen = e.pos;
            flags = 0u;
        }

        if ((pos + PFC_BLKHDR + plen) > cap) {
            return PFC_E_BOUND;
        }
        crc = pfc_crc32(work->scratch, plen);
        pfc_put_u32(&d[pos], (uint32_t)plen);
        d[pos + 4u] = flags;
        pfc_put_u32(&d[pos + 5u], crc);
        pos += PFC_BLKHDR;
        {
            size_t i;
            for (i = 0u; i < plen; i++) {
                d[pos + i] = work->scratch[i];
            }
        }
        pos += plen;
    }

    *out = pos;
    return PFC_OK;
}

/* ------------------------------------------------------------------ decode ---- */

static void fill_band(void *dst, uint8_t bitdepth, uint32_t width, uint32_t y0, uint32_t y1)
{
    uint32_t mid = (uint32_t)1u << (bitdepth - 1u);
    size_t base = (size_t)y0 * width;
    size_t n = (size_t)(y1 - y0) * width;
    size_t i;
    for (i = 0u; i < n; i++) {
        if (bitdepth > 8u) {
            ((uint16_t *)dst)[base + i] = (uint16_t)mid;
        } else {
            ((uint8_t *)dst)[base + i] = (uint8_t)mid;
        }
    }
}

pfc_status pfc_decode(const void *src, size_t src_len,
                      void *dst, size_t cap, size_t *out, pfc_ctx *work)
{
    const uint8_t *s = (const uint8_t *)src;
    uint8_t bitdepth, es;
    uint32_t width, height, band_rows, y0;
    size_t pos;
    int any_corrupt = 0;

    if ((src == NULL) || (dst == NULL) || (out == NULL) || (work == NULL)) {
        return PFC_E_PARAM;
    }
    if (src_len < PFC_HDR) {
        return PFC_E_CORRUPT;
    }
    if ((s[0] != 'P') || (s[1] != 'F') || (s[2] != 'C') || (s[3] != '1')) {
        return PFC_E_CORRUPT;
    }
    if (s[4] != (uint8_t)PFC_VERSION) {
        return PFC_E_UNSUPPORTED;
    }
    if (s[5] != (uint8_t)PFC_CODEC_IMAGE) {
        return PFC_E_UNSUPPORTED;
    }
    bitdepth = s[6];
    if ((bitdepth != 8u) && (bitdepth != 16u)) {
        return PFC_E_CORRUPT;
    }
    width = pfc_get_u32(&s[8]);
    height = pfc_get_u32(&s[12]);
    band_rows = pfc_get_u32(&s[16]);
    if ((width == 0u) || (height == 0u) || (band_rows == 0u) || (width > PFC_MAX_COLS)) {
        return PFC_E_CORRUPT;
    }
    es = elem_size(bitdepth);
    if (cap < ((size_t)width * height * es)) {
        return PFC_E_BOUND;
    }

    pos = PFC_HDR;
    for (y0 = 0u; y0 < height; y0 += band_rows) {
        uint32_t y1 = y0 + band_rows;
        uint32_t plen, crc;
        uint8_t flags;
        const uint8_t *payload;

        if (y1 > height) {
            y1 = height;
        }
        if ((pos + PFC_BLKHDR) > src_len) {
            fill_band(dst, bitdepth, width, y0, y1);
            any_corrupt = 1;
            continue;            /* truncated header: repair remaining bands */
        }
        plen = pfc_get_u32(&s[pos]);
        flags = s[pos + 4u];
        crc = pfc_get_u32(&s[pos + 5u]);
        payload = &s[pos + PFC_BLKHDR];

        if ((pos + PFC_BLKHDR + plen) > src_len) {
            fill_band(dst, bitdepth, width, y0, y1);
            any_corrupt = 1;
            continue;            /* implausible length: repair, do not read OOB (R6) */
        }
        if (pfc_crc32(payload, plen) != crc) {
            fill_band(dst, bitdepth, width, y0, y1);
            any_corrupt = 1;
            pos += PFC_BLKHDR + plen;
            continue;
        }

        if ((flags & 1u) != 0u) {
            size_t expect = (size_t)(y1 - y0) * width * es;
            if (plen != expect) {
                fill_band(dst, bitdepth, width, y0, y1);
                any_corrupt = 1;
            } else {
                pfc_image_load_raw(payload, dst, width, bitdepth, y0, y1);
            }
        } else {
            pfc_rc_dec dec;
            pfc_rc_dec_init(&dec, payload, plen);
            pfc_image_decode_band(&dec, work, dst, width, bitdepth, y0, y1);
        }
        pos += PFC_BLKHDR + plen;
    }

    *out = (size_t)width * height * es;
    return any_corrupt ? PFC_E_CORRUPT : PFC_OK;
}
