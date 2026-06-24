/* pfc_arith.c — 32-bit carryless range coder (Subbotin-style).
 * SPDX-License-Identifier: Apache-2.0
 *
 * Correctness condition: `tot` < PFC_RC_BOT on every call (the adaptive model caps its total at
 * PFC_MODEL_MAX < PFC_RC_BOT, and bypass coding uses tot=2). Integer-only, deterministic.
 */
#include "pfc_internal.h"

/* ---------------------------------------------------------------- encoder ---- */

void pfc_rc_enc_init(pfc_rc_enc *e, uint8_t *out, size_t cap)
{
    e->low = 0u;
    e->range = 0xFFFFFFFFu;
    e->out = out;
    e->cap = cap;
    e->pos = 0u;
    e->overflow = 0;
}

static void pfc_rc_put(pfc_rc_enc *e, uint8_t b)
{
    if (e->pos < e->cap) {
        e->out[e->pos] = b;
    } else {
        e->overflow = 1;
    }
    e->pos++;
}

static void pfc_rc_enc_renorm(pfc_rc_enc *e)
{
    while (((e->low ^ (e->low + e->range)) < PFC_RC_TOP) ||
           ((e->range < PFC_RC_BOT) &&
            ((e->range = (0u - e->low) & (PFC_RC_BOT - 1u)), 1))) {
        pfc_rc_put(e, (uint8_t)(e->low >> 24));
        e->low <<= 8;
        e->range <<= 8;
    }
}

void pfc_rc_encode(pfc_rc_enc *e, uint32_t cum, uint32_t freq, uint32_t tot)
{
    e->range /= tot;
    e->low += cum * e->range;
    e->range *= freq;
    pfc_rc_enc_renorm(e);
}

void pfc_rc_encode_bits(pfc_rc_enc *e, uint32_t bits, unsigned nbits)
{
    unsigned i;
    for (i = nbits; i > 0u; i--) {
        uint32_t bit = (bits >> (i - 1u)) & 1u;
        pfc_rc_encode(e, bit, 1u, 2u);
    }
}

void pfc_rc_enc_flush(pfc_rc_enc *e)
{
    int i;
    for (i = 0; i < 4; i++) {
        pfc_rc_put(e, (uint8_t)(e->low >> 24));
        e->low <<= 8;
    }
}

/* ---------------------------------------------------------------- decoder ---- */

static uint8_t pfc_rc_get(pfc_rc_dec *d)
{
    uint8_t b = 0u;
    if (d->pos < d->len) {
        b = d->in[d->pos];
        d->pos++;
    }
    return b;
}

void pfc_rc_dec_init(pfc_rc_dec *d, const uint8_t *in, size_t len)
{
    int i;
    d->low = 0u;
    d->range = 0xFFFFFFFFu;
    d->code = 0u;
    d->in = in;
    d->len = len;
    d->pos = 0u;
    for (i = 0; i < 4; i++) {
        d->code = (d->code << 8) | pfc_rc_get(d);
    }
}

uint32_t pfc_rc_getfreq(pfc_rc_dec *d, uint32_t tot)
{
    d->range /= tot;
    return (d->code - d->low) / d->range;
}

static void pfc_rc_dec_renorm(pfc_rc_dec *d)
{
    while (((d->low ^ (d->low + d->range)) < PFC_RC_TOP) ||
           ((d->range < PFC_RC_BOT) &&
            ((d->range = (0u - d->low) & (PFC_RC_BOT - 1u)), 1))) {
        d->code = (d->code << 8) | pfc_rc_get(d);
        d->low <<= 8;
        d->range <<= 8;
    }
}

void pfc_rc_decode_update(pfc_rc_dec *d, uint32_t cum, uint32_t freq, uint32_t tot)
{
    (void)tot; /* range already divided by tot in pfc_rc_getfreq */
    d->low += cum * d->range;
    d->range *= freq;
    pfc_rc_dec_renorm(d);
}

uint32_t pfc_rc_decode_bits(pfc_rc_dec *d, unsigned nbits)
{
    uint32_t v = 0u;
    unsigned i;
    for (i = 0u; i < nbits; i++) {
        uint32_t bit = pfc_rc_getfreq(d, 2u);
        pfc_rc_decode_update(d, bit, 1u, 2u);
        v = (v << 1) | bit;
    }
    return v;
}
