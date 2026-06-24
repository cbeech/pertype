/* pfc.c — public API: validation + dispatch over per-codec modules.
 * SPDX-License-Identifier: Apache-2.0
 *
 * Every codec emits the shared 20-byte header ('P''F''C''1' | ver | codec | 14 codec-param bytes)
 * followed by independently-CRC'd block records (see pfc_frame.c). Decode validates magic/version,
 * then dispatches on the codec byte.
 */
#include "pfc_internal.h"

size_t pfc_workmem_bytes(void)
{
    return sizeof(struct pfc_ctx);
}

size_t pfc_bound(pfc_codec codec, size_t n_in)
{
    (void)codec;
    return n_in + (n_in / 2u) + 1024u;
}

pfc_status pfc_encode(pfc_codec codec, const pfc_params *p,
                      const void *src, size_t src_len,
                      void *dst, size_t cap, size_t *out, pfc_ctx *work)
{
    uint8_t *d = (uint8_t *)dst;
    size_t pos = 0u;
    pfc_status st;

    if ((p == NULL) || (src == NULL) || (dst == NULL) || (out == NULL) || (work == NULL)) {
        return PFC_E_PARAM;
    }

    switch (codec) {
    case PFC_CODEC_IMAGE: {
        uint8_t es = (p->bitdepth > 8u) ? 2u : 1u;
        if (src_len != ((size_t)p->width * p->height * es)) {
            return PFC_E_PARAM;
        }
        st = pfc_image_encode(p, src, d, cap, &pos, work);
        break;
    }
    case PFC_CODEC_SEQ: {
        if ((p->elem != 1u) && (p->elem != 2u) && (p->elem != 4u)) {
            return PFC_E_PARAM;
        }
        if (src_len != ((size_t)p->count * p->elem)) {
            return PFC_E_PARAM;
        }
        st = pfc_seq_encode(p, src, d, cap, &pos, work);
        break;
    }
    case PFC_CODEC_FLOAT: {
        if ((p->elem != 4u) && (p->elem != 8u)) {
            return PFC_E_PARAM;
        }
        if (src_len != ((size_t)p->count * p->elem)) {
            return PFC_E_PARAM;
        }
        st = pfc_columnar_encode((uint8_t)codec, p, src, d, cap, &pos, work);
        break;
    }
    case PFC_CODEC_COLUMNAR: {
        if ((p->width == 0u) || (p->width > PFC_BLOCK_BYTES)) {
            return PFC_E_PARAM;
        }
        if (src_len != ((size_t)p->width * p->count)) {
            return PFC_E_PARAM;
        }
        st = pfc_columnar_encode((uint8_t)codec, p, src, d, cap, &pos, work);
        break;
    }
    default:
        return PFC_E_UNSUPPORTED;
    }

    if (st == PFC_OK) {
        *out = pos;
    }
    return st;
}

pfc_status pfc_decode(const void *src, size_t src_len,
                      void *dst, size_t cap, size_t *out, pfc_ctx *work)
{
    const uint8_t *s = (const uint8_t *)src;
    int corrupt = 0;
    pfc_status st;

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

    switch ((pfc_codec)s[5]) {
    case PFC_CODEC_IMAGE:
        st = pfc_image_decode(s, src_len, dst, cap, out, work, &corrupt);
        break;
    case PFC_CODEC_SEQ:
        st = pfc_seq_decode(s, src_len, dst, cap, out, work, &corrupt);
        break;
    case PFC_CODEC_FLOAT:
    case PFC_CODEC_COLUMNAR:
        st = pfc_columnar_decode(s, src_len, dst, cap, out, work, &corrupt);
        break;
    default:
        return PFC_E_UNSUPPORTED;
    }

    if (st != PFC_OK) {
        return st;
    }
    return corrupt ? PFC_E_CORRUPT : PFC_OK;
}
