/* pfc_internal.h — internal contracts shared across the flight core.
 * SPDX-License-Identifier: Apache-2.0
 *
 * MISRA/JPL discipline: integer-only, no recursion, no dynamic allocation, bounded loops.
 */
#ifndef PFC_INTERNAL_H
#define PFC_INTERNAL_H

#include "pfc.h"

/* Range coder renormalisation constants (32-bit carryless coder). */
#define PFC_RC_TOP ((uint32_t)1u << 24)
#define PFC_RC_BOT ((uint32_t)1u << 16)

/* Adaptive model tuning. MODEL_MAX keeps the total below PFC_RC_BOT for coder correctness. */
#define PFC_MODEL_INC 24u
#define PFC_MODEL_MAX ((uint32_t)1u << 13)

/* Opaque working-memory layout (caller allocates one, statically). */
struct pfc_ctx {
    uint16_t freq[PFC_NCTX][PFC_NSYM];   /* adaptive category frequencies, per context */
    uint32_t tot[PFC_NCTX];              /* per-context totals */
    uint8_t  scratch[PFC_SCRATCH_BYTES]; /* one band's range-coded / store-raw payload */
};

/* ---- range encoder ---- */
typedef struct {
    uint32_t low;
    uint32_t range;
    uint8_t *out;
    size_t   cap;
    size_t   pos;     /* bytes produced (may exceed cap; then `overflow` is set) */
    int      overflow;
} pfc_rc_enc;

void     pfc_rc_enc_init(pfc_rc_enc *e, uint8_t *out, size_t cap);
void     pfc_rc_encode(pfc_rc_enc *e, uint32_t cum, uint32_t freq, uint32_t tot);
void     pfc_rc_encode_bits(pfc_rc_enc *e, uint32_t bits, unsigned nbits);
void     pfc_rc_enc_flush(pfc_rc_enc *e);

/* ---- range decoder ---- */
typedef struct {
    uint32_t low;
    uint32_t range;
    uint32_t code;
    const uint8_t *in;
    size_t   len;
    size_t   pos;
} pfc_rc_dec;

void     pfc_rc_dec_init(pfc_rc_dec *d, const uint8_t *in, size_t len);
uint32_t pfc_rc_getfreq(pfc_rc_dec *d, uint32_t tot);
void     pfc_rc_decode_update(pfc_rc_dec *d, uint32_t cum, uint32_t freq, uint32_t tot);
uint32_t pfc_rc_decode_bits(pfc_rc_dec *d, unsigned nbits);

/* ---- adaptive category model + residual coding ---- */
void     pfc_model_reset(pfc_ctx *w);
void     pfc_resid_encode(pfc_rc_enc *e, pfc_ctx *w, unsigned *prev_k, int32_t resid);
int32_t  pfc_resid_decode(pfc_rc_dec *d, pfc_ctx *w, unsigned *prev_k);

/* ---- CRC-32 (IEEE 802.3, reflected) ---- */
uint32_t pfc_crc32(const uint8_t *buf, size_t len);

/* ---- image (MED) band codec ---- */
/* Encode rows [y0,y1) of a width*height plane into e; returns nothing (check e->overflow). */
void     pfc_image_encode_band(pfc_rc_enc *e, pfc_ctx *w, const void *src,
                               uint32_t width, uint8_t bitdepth, uint32_t y0, uint32_t y1);
/* Decode one band from d into dst rows [y0,y1). */
void     pfc_image_decode_band(pfc_rc_dec *d, pfc_ctx *w, void *dst,
                               uint32_t width, uint8_t bitdepth, uint32_t y0, uint32_t y1);
/* Store-raw helpers (canonical little-endian samples), used for the no-expansion fallback. */
void     pfc_image_store_raw(uint8_t *out, const void *src, uint32_t width, uint8_t bitdepth,
                             uint32_t y0, uint32_t y1);
void     pfc_image_load_raw(const uint8_t *in, void *dst, uint32_t width, uint8_t bitdepth,
                            uint32_t y0, uint32_t y1);

/* ---- little-endian serialisation helpers (endianness-neutral wire format, R4) ---- */
static inline void pfc_put_u32(uint8_t *p, uint32_t v) {
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)((v >> 8) & 0xFFu);
    p[2] = (uint8_t)((v >> 16) & 0xFFu);
    p[3] = (uint8_t)((v >> 24) & 0xFFu);
}
static inline uint32_t pfc_get_u32(const uint8_t *p) {
    return (uint32_t)p[0] | ((uint32_t)p[1] << 8) |
           ((uint32_t)p[2] << 16) | ((uint32_t)p[3] << 24);
}

#endif /* PFC_INTERNAL_H */
