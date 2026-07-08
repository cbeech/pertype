/* emit.c — encode deterministic fixtures and write the streams to stdout.
 * SPDX-License-Identifier: Apache-2.0
 *
 * Used by CI to PROVE the wire format is endianness-neutral (R4): build this for a big-endian
 * target (e.g. powerpc-linux-gnu) and for x86, run both, and `cmp` the outputs — identical bytes
 * means a big-endian flight encoder and a little-endian ground decoder produce/consume the same
 * stream. Deterministic (fixed LCG, no time/rand), so the output depends only on the codec, not
 * the platform. The library itself never mallocs (R2); this harness mallocs its context ONCE at
 * startup, sized by pfc_workmem_bytes() — the same pattern test_pfc.c/stress.c/fuzz_pfc.c use.
 * (pfc_ctx is intentionally opaque in the public pfc.h, so a plain `static struct pfc_ctx g_work;`
 * here would instantiate an incomplete type and fail to compile.)
 */
#include "pfc.h"
#include <stdio.h>
#include <stdlib.h>
#include <stdint.h>

static struct pfc_ctx *g_work;         /* malloc'd once in main(), sized by pfc_workmem_bytes() */
static uint8_t g_enc[1u << 20];        /* 1 MB output */

static uint32_t g_rng = 0x1234567u;
static uint32_t rng(void) { g_rng = g_rng * 1664525u + 1013904223u; return g_rng; }

static void emit(pfc_codec codec, const pfc_params *p, const void *src, size_t n)
{
    size_t out = 0;
    uint8_t hdr[4];
    if (pfc_encode(codec, p, src, n, g_enc, sizeof g_enc, &out, g_work) != PFC_OK) {
        fputs("encode failed\n", stderr);
        return;
    }
    hdr[0] = (uint8_t)(out & 0xFFu); hdr[1] = (uint8_t)((out >> 8) & 0xFFu);
    hdr[2] = (uint8_t)((out >> 16) & 0xFFu); hdr[3] = (uint8_t)((out >> 24) & 0xFFu);
    fwrite(hdr, 1, 4, stdout);          /* canonical LE length prefix */
    fwrite(g_enc, 1, out, stdout);
}

int main(void)
{
    static uint16_t img[64 * 48];
    static int16_t seq[2000];
    static uint16_t cube[16 * 16 * 8];
    pfc_params p;
    size_t i;

    g_work = malloc(pfc_workmem_bytes());
    if (g_work == NULL) {
        fputs("malloc failed\n", stderr);
        return 1;
    }

    for (i = 0; i < (64u * 48u); i++) { img[i] = (uint16_t)(((i * 7u) + (rng() >> 28)) & 0xFFFFu); }
    for (i = 0; i < 2000u; i++) { seq[i] = (int16_t)((int)(i % 500u) - 250); }
    for (i = 0; i < (16u * 16u * 8u); i++) {
        uint32_t b = (uint32_t)(i / (16u * 16u));
        cube[i] = (uint16_t)(((i * 5u) + b * 40u) & 0x0FFFu);
    }

    p.width = 64; p.height = 48; p.count = 0; p.bitdepth = 16; p.elem = 0; p.is_signed = 0;
    emit(PFC_CODEC_IMAGE, &p, img, sizeof img);

    p.width = 0; p.height = 0; p.count = 2000; p.bitdepth = 0; p.elem = 2; p.is_signed = 1;
    emit(PFC_CODEC_SEQ, &p, seq, sizeof seq);

    p.width = 16; p.height = 16; p.count = 8; p.bitdepth = 16; p.elem = 0; p.is_signed = 0;
    emit(PFC_CODEC_SPECTRAL, &p, cube, sizeof cube);

    return 0;
}
