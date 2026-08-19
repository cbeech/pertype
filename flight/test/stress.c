/* stress.c — aggressive verification harness (run under ASan/UBSan).
 * SPDX-License-Identifier: Apache-2.0
 *
 * Property tests (lossless + no-expansion + determinism) over thousands of randomised cases for all
 * four codecs, adversarial edge/extreme cases, too-small-buffer handling, and a decode fuzz loop.
 * Buffers are exact-sized mallocs so ASan catches any 1-byte over-read/over-write.
 */
#include "pfc.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static struct pfc_ctx *W;
static int g_pass = 0, g_fail = 0;
static uint32_t RS = 0x2468ACEu;

static uint32_t rnd(void) { RS = RS * 1664525u + 1013904223u; return RS; }
static uint32_t rr(uint32_t n) { return n ? (rnd() % n) : 0u; }

#define OK(cond, msg) do { if (cond) { g_pass++; } else { g_fail++; \
    printf("  FAIL: %s\n", msg); } } while (0)

enum { D_ZERO, D_CONST, D_RAMP, D_LOWF, D_NOISE, D_SPARSE, D_ALT, N_DIST };

static void fill(uint8_t *b, size_t n, int dist)
{
    size_t i;
    uint8_t c = (uint8_t)rnd();
    uint32_t acc = rnd();
    switch (dist) {
    case D_ZERO:   memset(b, 0, n); break;
    case D_CONST:  memset(b, (int)c, n); break;
    case D_RAMP:   for (i = 0; i < n; i++) { b[i] = (uint8_t)i; } break;
    case D_LOWF:   for (i = 0; i < n; i++) { acc += (uint32_t)((int)rr(7) - 3); b[i] = (uint8_t)(acc >> 2); } break;
    case D_NOISE:  for (i = 0; i < n; i++) { b[i] = (uint8_t)rnd(); } break;
    case D_SPARSE: memset(b, 0, n); for (i = 0; i < n; i++) { if (rr(16) == 0u) { b[i] = (uint8_t)rnd(); } } break;
    default:       for (i = 0; i < n; i++) { b[i] = (i & 1u) ? 0xFFu : 0x00u; } break;
    }
}

static void run(const char *tag, pfc_codec codec, pfc_params p, size_t n_in, int dist)
{
    size_t cap = pfc_bound(codec, n_in);
    uint8_t *src = malloc(n_in ? n_in : 1u);
    uint8_t *enc = malloc(cap ? cap : 1u);
    uint8_t *enc2 = malloc(cap ? cap : 1u);
    uint8_t *dec = malloc(n_in ? n_in : 1u);
    size_t out = 0, out2 = 0, dout = 0;
    pfc_status se, sd;

    fill(src, n_in, dist);
    se = pfc_encode(codec, &p, src, n_in, enc, cap, &out, W);
    if (se != PFC_OK) {
        g_fail++;
        printf("  FAIL: %s encode=%d (cap=%zu n_in=%zu) — bound too small?\n", tag, se, cap, n_in);
        goto done;
    }
    OK(out <= cap, "out <= pfc_bound (no expansion, R5)");

    sd = pfc_decode(enc, out, dec, n_in, &dout, W);
    OK((sd == PFC_OK) && (dout == n_in) && (memcmp(src, dec, n_in) == 0), tag);

    (void)pfc_encode(codec, &p, src, n_in, enc2, cap, &out2, W);
    OK((out2 == out) && (memcmp(enc, enc2, out) == 0), "deterministic");
done:
    free(src); free(enc); free(enc2); free(dec);
}

static void edges(void)
{
    pfc_params p;
    /* image extremes */
    memset(&p, 0, sizeof p); p.width = 1; p.height = 1; p.bitdepth = 16;
    run("img 1x1 16", PFC_CODEC_IMAGE, p, 2, D_NOISE);
    p.width = 1; p.height = 1; p.bitdepth = 8; run("img 1x1 8", PFC_CODEC_IMAGE, p, 1, D_NOISE);
    /* skinny incompressible images: the worst case for per-block framing overhead vs pfc_bound */
    p.width = 1; p.height = 30000; p.bitdepth = 8;
    run("img 1x30000 8 NOISE", PFC_CODEC_IMAGE, p, 30000, D_NOISE);
    p.width = 1; p.height = 30000; p.bitdepth = 16;
    run("img 1x30000 16 NOISE", PFC_CODEC_IMAGE, p, 60000, D_NOISE);
    p.width = 8000; p.height = 1; p.bitdepth = 8;        /* wide single row (within PFC_MAX_COLS) */
    run("img 8000x1 8 NOISE", PFC_CODEC_IMAGE, p, 8000, D_NOISE);
    p.width = 8192; p.height = 5; p.bitdepth = 16;        /* max width */
    run("img 8192x5 16", PFC_CODEC_IMAGE, p, (size_t)8192 * 5 * 2, D_LOWF);
    p.width = 64; p.height = 64; p.bitdepth = 16;
    run("img 64x64 ALT", PFC_CODEC_IMAGE, p, 64 * 64 * 2, D_ALT);

    /* seq */
    memset(&p, 0, sizeof p); p.count = 1; p.elem = 4; p.is_signed = 1;
    run("seq count1 i32", PFC_CODEC_SEQ, p, 4, D_NOISE);
    p.count = 40000; p.elem = 4; p.is_signed = 1;
    run("seq 40000 i32 NOISE", PFC_CODEC_SEQ, p, 160000, D_NOISE);     /* full-range delta wrap */
    p.count = 40000; p.elem = 4; p.is_signed = 1;
    run("seq 40000 i32 ALT", PFC_CODEC_SEQ, p, 160000, D_ALT);
    p.count = 50000; p.elem = 1; p.is_signed = 0;
    run("seq 50000 u8 NOISE", PFC_CODEC_SEQ, p, 50000, D_NOISE);

    /* float */
    memset(&p, 0, sizeof p); p.count = 1; p.elem = 8;
    run("float count1 f64", PFC_CODEC_FLOAT, p, 8, D_NOISE);
    p.count = 20000; p.elem = 4; run("float 20000 NOISE", PFC_CODEC_FLOAT, p, 80000, D_NOISE);

    /* columnar */
    memset(&p, 0, sizeof p); p.width = 1; p.count = 50000;
    run("col rw1 NOISE", PFC_CODEC_COLUMNAR, p, 50000, D_NOISE);
    p.width = 64; p.count = 4000; run("col rw64 LOWF", PFC_CODEC_COLUMNAR, p, (size_t)64 * 4000, D_LOWF);
    p.width = 1; p.count = 1; run("col 1x1", PFC_CODEC_COLUMNAR, p, 1, D_NOISE);
}

static void too_small_buffers(void)
{
    pfc_params p; uint8_t src[4096]; uint8_t dst[64]; size_t out = 0; uint32_t i;
    memset(&p, 0, sizeof p); p.width = 64; p.height = 32; p.bitdepth = 8;
    for (i = 0; i < 4096u; i++) { src[i] = (uint8_t)rnd(); }   /* incompressible */
    /* every undersized cap must return a status, never overflow dst (ASan-checked) */
    for (i = 0; i <= 64u; i += 8u) {
        pfc_status st = pfc_encode(PFC_CODEC_IMAGE, &p, src, 64u * 32u, dst, i, &out, W);
        OK((st == PFC_E_BOUND) || (st == PFC_OK), "undersized cap -> status, no overflow");
    }
}

static void negatives(void)
{
    uint8_t s[64], d[64]; size_t out = 0; pfc_params q; uint32_t i;
    for (i = 0; i < 64u; i++) { s[i] = (uint8_t)rnd(); }
    memset(&q, 0, sizeof q); q.width = 4; q.height = 4; q.bitdepth = 7;
    OK(pfc_encode(PFC_CODEC_IMAGE, &q, s, 16, d, 64, &out, W) == PFC_E_PARAM, "bad bitdepth rejected");
    memset(&q, 0, sizeof q); q.width = 9000; q.height = 1; q.bitdepth = 8;   /* > PFC_MAX_COLS */
    OK(pfc_encode(PFC_CODEC_IMAGE, &q, s, 9000, d, 64, &out, W) == PFC_E_PARAM, "width>MAX rejected");
    memset(&q, 0, sizeof q); q.count = 8; q.elem = 3;
    OK(pfc_encode(PFC_CODEC_SEQ, &q, s, 24, d, 64, &out, W) == PFC_E_PARAM, "bad seq elem rejected");
    memset(&q, 0, sizeof q); q.elem = 4; q.count = 8;
    OK(pfc_encode(PFC_CODEC_FLOAT, &q, s, 8, d, 64, &out, W) == PFC_E_PARAM, "float len mismatch rejected");
    OK(pfc_encode(PFC_CODEC_IMAGE, &q, NULL, 16, d, 64, &out, W) == PFC_E_PARAM, "NULL src rejected");
    OK(pfc_decode(s, 3, d, 64, &out, W) == PFC_E_CORRUPT, "too-short stream rejected");
}

static void random_cases(uint32_t n)
{
    uint32_t it;
    for (it = 0; it < n; it++) {
        pfc_params p; size_t n_in; pfc_codec codec = (pfc_codec)(1u + rr(5u));
        int dist = (int)rr(N_DIST);
        memset(&p, 0, sizeof p);
        if (codec == PFC_CODEC_SPECTRAL) {
            uint8_t bd = (rr(2u) != 0u) ? 16u : 8u; uint8_t es = (bd > 8u) ? 2u : 1u;
            p.bitdepth = bd;
            p.width = 1u + rr(60u); p.height = 1u + rr(60u); p.count = 1u + rr(40u);
            n_in = (size_t)p.width * p.height * p.count * es;
        } else if (codec == PFC_CODEC_IMAGE) {
            uint8_t bd = (rr(2u) != 0u) ? 16u : 8u; uint8_t es = (bd > 8u) ? 2u : 1u;
            p.bitdepth = bd;
            if (rr(8u) == 0u) { p.width = 1u; p.height = 1u + rr(2000u); }   /* skinny */
            else { p.width = 1u + rr(260u); p.height = 1u + rr(260u); }
            n_in = (size_t)p.width * p.height * es;
        } else if (codec == PFC_CODEC_SEQ) {
            uint8_t es = (uint8_t)(1u << rr(3u));   /* 1,2,4 */
            p.elem = es; p.is_signed = (uint8_t)rr(2u); p.count = 1u + rr(30000u);
            n_in = (size_t)p.count * es;
        } else if (codec == PFC_CODEC_FLOAT) {
            uint8_t es = (rr(2u) != 0u) ? 8u : 4u;
            p.elem = es; p.count = 1u + rr(15000u); n_in = (size_t)p.count * es;
        } else {
            p.width = 1u + rr(80u); p.count = 1u + rr(4000u); n_in = (size_t)p.width * p.count;
        }
        run("random", codec, p, n_in, dist);
    }
}

static void fuzz_decode(uint32_t n)
{
    static uint8_t dst[1u << 21];   /* 2 MB */
    uint8_t buf[8192];
    uint32_t it, i, m;
    size_t out = 0;
    for (it = 0; it < n; it++) {
        size_t len = rr(4096u);
        for (i = 0; i < len; i++) { buf[i] = (uint8_t)rnd(); }
        if (rr(2u) != 0u) {            /* sometimes start with a valid-looking header */
            buf[0] = 'P'; buf[1] = 'F'; buf[2] = 'C'; buf[3] = '1';
            buf[4] = 1u; buf[5] = (uint8_t)(1u + rr(4u));
        }
        m = 1u + rr(8u);
        for (i = 0; i < m; i++) { if (len) { buf[rr((uint32_t)len)] = (uint8_t)rnd(); } }
        (void)pfc_decode(buf, len, dst, sizeof dst, &out, W);   /* must never crash/OOB */
    }
}

int main(int argc, char **argv)
{
    uint32_t nrand = (argc > 1) ? (uint32_t)atoi(argv[1]) : 4000u;
    uint32_t nfuzz = (argc > 2) ? (uint32_t)atoi(argv[2]) : 100000u;
    W = malloc(pfc_workmem_bytes());
    printf("stress: %u random cases + edges + %u fuzz  (workmem %zu B)\n",
           nrand, nfuzz, pfc_workmem_bytes());
    edges();
    negatives();
    too_small_buffers();
    random_cases(nrand);
    fuzz_decode(nfuzz);
    free(W);
    printf("\n%d passed, %d failed\n", g_pass, g_fail);
    return (g_fail == 0) ? 0 : 1;
}
