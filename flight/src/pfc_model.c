/* pfc_model.c — adaptive magnitude-category model + residual coding.
 * SPDX-License-Identifier: Apache-2.0
 *
 * A prediction residual is coded as (category k, mantissa bits), JPEG/Exp-Golomb style:
 *   u = zigzag(resid); k = bitlength(u); k is entropy-coded with a small adaptive model
 *   (context = clamped previous category); the low (k-1) mantissa bits are coded bypass.
 * Exact and reversible: u=0 -> k=0; otherwise the top bit is implied by k.
 */
#include "pfc_internal.h"

void pfc_model_reset(pfc_ctx *w)
{
    unsigned c, s;
    for (c = 0u; c < PFC_NCTX; c++) {
        for (s = 0u; s < PFC_NSYM; s++) {
            w->freq[c][s] = 1u;     /* Laplace start: every category possible */
        }
        w->tot[c] = PFC_NSYM;
    }
}

static void pfc_model_rescale(pfc_ctx *w, unsigned ctx)
{
    unsigned s;
    uint32_t t = 0u;
    for (s = 0u; s < PFC_NSYM; s++) {
        uint16_t f = (uint16_t)((w->freq[ctx][s] + 1u) >> 1); /* halve, keep >=1 */
        w->freq[ctx][s] = f;
        t += f;
    }
    w->tot[ctx] = t;
}

static void pfc_model_update(pfc_ctx *w, unsigned ctx, unsigned k)
{
    w->freq[ctx][k] = (uint16_t)(w->freq[ctx][k] + PFC_MODEL_INC);
    w->tot[ctx] += PFC_MODEL_INC;
    if (w->tot[ctx] >= PFC_MODEL_MAX) {
        pfc_model_rescale(w, ctx);
    }
}

static uint32_t pfc_zigzag(int32_t n)
{
    return ((uint32_t)n << 1) ^ (uint32_t)(n >> 31);
}

static int32_t pfc_unzigzag(uint32_t u)
{
    return (int32_t)((u >> 1) ^ (0u - (u & 1u)));
}

static unsigned pfc_bitlen(uint32_t u)
{
    unsigned k = 0u;
    while (u != 0u) {
        k++;
        u >>= 1;
    }
    return k;
}

unsigned pfc_cat(int32_t resid)
{
    unsigned k = pfc_bitlen(pfc_zigzag(resid));
    return (k < PFC_NCTX) ? k : (PFC_NCTX - 1u);
}

void pfc_resid_encode(pfc_rc_enc *e, pfc_ctx *w, unsigned ctx, int32_t resid)
{
    uint32_t u = pfc_zigzag(resid);
    unsigned k = pfc_bitlen(u);             /* 0..PFC_KMAX */
    uint32_t cum = 0u;
    unsigned s;

    for (s = 0u; s < k; s++) {
        cum += w->freq[ctx][s];
    }
    pfc_rc_encode(e, cum, w->freq[ctx][k], w->tot[ctx]);
    if (k > 1u) {
        pfc_rc_encode_bits(e, u & (((uint32_t)1u << (k - 1u)) - 1u), k - 1u);
    }
    pfc_model_update(w, ctx, k);
}

int32_t pfc_resid_decode(pfc_rc_dec *d, pfc_ctx *w, unsigned ctx)
{
    uint32_t target = pfc_rc_getfreq(d, w->tot[ctx]);
    uint32_t cum = 0u;
    unsigned k = 0u;
    uint32_t u;

    while ((k < PFC_KMAX) && ((cum + w->freq[ctx][k]) <= target)) {
        cum += w->freq[ctx][k];
        k++;
    }
    pfc_rc_decode_update(d, cum, w->freq[ctx][k], w->tot[ctx]);

    if (k == 0u) {
        u = 0u;
    } else if (k == 1u) {
        u = 1u;
    } else {
        uint32_t low = pfc_rc_decode_bits(d, k - 1u);
        u = ((uint32_t)1u << (k - 1u)) | low;
    }
    pfc_model_update(w, ctx, k);
    return pfc_unzigzag(u);
}
