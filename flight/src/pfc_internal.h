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

/* Hard iteration bound for the renormalisation loops (pfc_arith.c).
 *
 * JPL Power-of-Ten Rule 2 requires every loop to have a statically provable upper bound. The
 * renorm loops are `while` loops whose termination depends on the VALUES of low/range, so they had
 * no such bound -- and that was not a theoretical gap. SEU fault injection (test/seu_inject.c)
 * demonstrated a concrete hang: a single flipped bit that zeroes a `freq[][]` entry makes
 * `pfc_rc_encode` compute `range = (range/tot) * 0 == 0`, after which `low ^ (low + 0) == 0` is
 * permanently below PFC_RC_TOP and `range <<= 8` can never restore it. The loop spins forever --
 * on a spacecraft that is a hung compression task and a watchdog reset, a strictly worse outcome
 * than the silent data corruption docs/mission-safety.md 2.5 anticipated.
 *
 * freq >= 1 is an invariant in normal operation (pfc_model_reset seeds every entry >= 1,
 * pfc_model_rescale's (f+1)>>1 cannot reach 0, pfc_model_update only adds), so this bound is
 * unreachable on uncorrupted state and changes no output. It exists to convert an unbounded hang
 * under memory corruption into a bounded, detectable failure.
 *
 * 8 is generous: each iteration shifts low and range left by 8 bits, so a 32-bit value is fully
 * consumed in 4: legitimate renormalisation cannot need more. Verified empirically -- the full
 * test corpus produces byte-identical output with this bound in place. */
#define PFC_RC_RENORM_MAX 8u

/* Adaptive model tuning. MODEL_MAX keeps the total below PFC_RC_BOT for coder correctness. */
#define PFC_MODEL_INC 24u
#define PFC_MODEL_MAX ((uint32_t)1u << 13)
/* Adaptive model for the top mantissa bit per category (residuals within a bin aren't uniform). */
#define PFC_MANT_INC 24u
#define PFC_MANT_MAX ((uint32_t)1u << 12)

/* Block size for the 1-D / float / columnar codecs (raw bytes per independently-coded block). */
#define PFC_BLOCK_BYTES 65536u

/* Block-record framing overhead: payload_len(4) | flags(1) | crc32(4). */
#define PFC_BLKHDR 9u
#define PFC_BLK_FLAG_RAW 1u

/* Opaque working-memory layout (caller allocates one, statically). */
struct pfc_ctx {
    uint16_t freq[PFC_NCTX][PFC_NSYM];   /* adaptive category frequencies, per context */
    uint32_t tot[PFC_NCTX];              /* per-context totals */
    uint16_t mant[PFC_NSYM][2];          /* adaptive top-mantissa-bit model, per category */
    int16_t  bias_c[PFC_NCTX];           /* image: per-context bias correction (LOCO-I C) */
    int32_t  bias_b[PFC_NCTX];           /* image: accumulated error (LOCO-I B) */
    int32_t  bias_n[PFC_NCTX];           /* image: context occurrence count (LOCO-I N) */
    uint8_t  scratch[PFC_SCRATCH_BYTES]; /* one block's range-coded / store-raw payload */
    uint8_t  xform[PFC_BLOCK_BYTES];     /* de-interleave / delta workspace (non-image codecs) */
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

/* ---- adaptive category model + residual coding (caller supplies the context) ---- */
void     pfc_model_reset(pfc_ctx *w);
void     pfc_resid_encode(pfc_rc_enc *e, pfc_ctx *w, unsigned ctx, int32_t resid);
int32_t  pfc_resid_decode(pfc_rc_dec *d, pfc_ctx *w, unsigned ctx);
void     pfc_uint_encode(pfc_rc_enc *e, pfc_ctx *w, unsigned ctx, uint32_t u);  /* raw count, no zigzag */
uint32_t pfc_uint_decode(pfc_rc_dec *d, pfc_ctx *w, unsigned ctx);
unsigned pfc_cat(int32_t resid);   /* clamped magnitude category, for 1-D/columnar contexts */

/* image run-mode entropy contexts (reserved high indices; emc<=18 and seq<=32 never reach them) */
#define PFC_CTX_RUNLEN (PFC_NCTX - 2u)
#define PFC_CTX_RUNINT (PFC_NCTX - 1u)

/* ---- CRC-32 (IEEE 802.3, reflected) ---- */
uint32_t pfc_crc32(const uint8_t *buf, size_t len);

/* ---- block framing helpers (shared by all codecs) ----
 * Write one block record (len|flags|crc|payload) into dst at *pos; bumps *pos. */
pfc_status pfc_block_write(uint8_t *dst, size_t cap, size_t *pos,
                           const uint8_t *payload, size_t plen, uint8_t flags);
/* Read one block record from src at pos; on success sets payload, plen, flags and bumps pos.
 * Returns PFC_E_CORRUPT (without advancing OOB) on truncation or CRC mismatch. */
pfc_status pfc_block_read(const uint8_t *src, size_t len, size_t *pos,
                          const uint8_t **payload, size_t *plen, uint8_t *flags);

/* ---- image (MED) band codec ---- */
void     pfc_image_encode_band(pfc_rc_enc *e, pfc_ctx *w, const void *src,
                               uint32_t width, uint8_t bitdepth, uint32_t y0, uint32_t y1);
void     pfc_image_decode_band(pfc_rc_dec *d, pfc_ctx *w, void *dst,
                               uint32_t width, uint8_t bitdepth, uint32_t y0, uint32_t y1);
void     pfc_image_store_raw(uint8_t *out, const void *src, uint32_t width, uint8_t bitdepth,
                             uint32_t y0, uint32_t y1);
void     pfc_image_load_raw(const uint8_t *in, void *dst, uint32_t width, uint8_t bitdepth,
                            uint32_t y0, uint32_t y1);

/* ---- per-codec encode/decode (each writes/reads its own stream after the shared header) ---- */
pfc_status pfc_image_encode(const pfc_params *p, const void *src, uint8_t *dst, size_t cap,
                            size_t *pos, pfc_ctx *w);
pfc_status pfc_image_decode(const uint8_t *s, size_t len, void *dst, size_t cap,
                            size_t *out, pfc_ctx *w, int *corrupt);

pfc_status pfc_seq_encode(const pfc_params *p, const void *src, uint8_t *dst, size_t cap,
                          size_t *pos, pfc_ctx *w);
pfc_status pfc_seq_decode(const uint8_t *s, size_t len, void *dst, size_t cap,
                          size_t *out, pfc_ctx *w, int *corrupt);

pfc_status pfc_columnar_encode(uint8_t codec, const pfc_params *p, const void *src,
                               uint8_t *dst, size_t cap, size_t *pos, pfc_ctx *w);
pfc_status pfc_columnar_decode(const uint8_t *s, size_t len, void *dst, size_t cap,
                               size_t *out, pfc_ctx *w, int *corrupt);

pfc_status pfc_spectral_encode(const pfc_params *p, const void *src, uint8_t *dst, size_t cap,
                               size_t *pos, pfc_ctx *w);
pfc_status pfc_spectral_decode(const uint8_t *s, size_t len, void *dst, size_t cap,
                               size_t *out, pfc_ctx *w, int *corrupt);

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

/* shared stream header size: magic(4) ver(1) codec(1) + 14 codec-param bytes */
#define PFC_HDR 20u

/* Multiply a*b without ever wrapping size_t; returns 0 (and leaves *out untouched) if the true
 * product doesn't fit, 1 otherwise. Every decoder computes an expected output size by chaining
 * several untrusted wire-header fields (e.g. SPECTRAL's width*height*count*elemsize is up to 78
 * bits of untrusted input), and a naive `a * b` there can wrap size_t and let a too-small `cap`
 * check pass -- on any width, not just the 32-bit one pfc_block_read's fix targeted: SPECTRAL's
 * four-factor product can overflow even a 64-bit size_t (13+32+32+1 = 78 bits). Chain calls
 * (each feeding the previous *out back in as a) to safely multiply more than two factors.
 *
 * The standard CERT C INT30-C division-guarded-multiply idiom, correct by construction (a is
 * unsigned, integer division truncates toward zero, i.e. rounds down):
 *   a == 0: product is 0, always representable -- returns 1 with *out = 0.
 *   a != 0, b <= SIZE_MAX/a: since integer division rounds down, a * (SIZE_MAX/a) <= SIZE_MAX,
 *     and b <= SIZE_MAX/a implies a*b <= a*(SIZE_MAX/a) <= SIZE_MAX -- fits, returns 1.
 *   a != 0, b > SIZE_MAX/a: b >= (SIZE_MAX/a) + 1, so a*b >= a*(SIZE_MAX/a) + a > SIZE_MAX -- the
 *     true product exceeds SIZE_MAX, so it cannot fit -- correctly returns 0 without computing
 *     `a*b` at all (avoiding the wraparound this function exists to prevent).
 * Attempted a CBMC proof of this over the full unconstrained size_t domain (both --32 and native
 * 64-bit); the fully-generic multiplication-overflow SAT problem didn't converge within a 10-
 * minute budget on either width. Not worth an unreliable/slow CI gate for a well-established,
 * hand-verifiable four-line idiom -- see docs/requirements.md. */
static inline int pfc_size_mul(size_t a, size_t b, size_t *out) {
    if ((a != 0u) && (b > (SIZE_MAX / a))) {
        return 0;
    }
    *out = a * b;
    return 1;
}

#endif /* PFC_INTERNAL_H */
