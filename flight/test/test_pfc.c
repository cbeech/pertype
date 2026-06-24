/* test_pfc.c — host test suite for libpfc.
 * SPDX-License-Identifier: Apache-2.0
 *
 * Covers: R1 round-trip (8/16-bit, odd sizes), R5 no-expansion + store-raw, R6 error containment
 * (bit-flip + truncation), and a compression-ratio sanity check on smooth data.
 */
#include "pfc.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int g_pass = 0, g_fail = 0;
#define CHECK(cond, msg) do { \
    if (cond) { g_pass++; } \
    else { g_fail++; printf("  FAIL: %s  (%s:%d)\n", msg, __FILE__, __LINE__); } \
} while (0)

static struct pfc_ctx *g_work; /* allocated once on the host (library itself never mallocs) */

static size_t elem(uint8_t bd) { return (bd > 8u) ? 2u : 1u; }

static void *make_gradient(uint32_t w, uint32_t h, uint8_t bd)
{
    size_t n = (size_t)w * h;
    uint32_t mask = (bd > 8u) ? 0xFFFFu : 0xFFu;
    size_t i;
    void *p = malloc(n * elem(bd));
    for (i = 0; i < n; i++) {
        uint32_t x = (uint32_t)(i % w), y = (uint32_t)(i / w);
        uint32_t v = ((x * 3u + y * 7u) & mask);
        if (bd > 8u) ((uint16_t *)p)[i] = (uint16_t)v; else ((uint8_t *)p)[i] = (uint8_t)v;
    }
    return p;
}

static void *make_random(uint32_t w, uint32_t h, uint8_t bd, uint32_t seed)
{
    size_t n = (size_t)w * h;
    uint32_t mask = (bd > 8u) ? 0xFFFFu : 0xFFu;
    uint32_t s = seed ? seed : 1u;
    size_t i;
    void *p = malloc(n * elem(bd));
    for (i = 0; i < n; i++) {
        s = s * 1664525u + 1013904223u;            /* LCG, deterministic */
        if (bd > 8u) ((uint16_t *)p)[i] = (uint16_t)((s >> 8) & mask);
        else ((uint8_t *)p)[i] = (uint8_t)((s >> 8) & mask);
    }
    return p;
}

static void roundtrip(const char *name, uint32_t w, uint32_t h, uint8_t bd, void *img, int expect_small)
{
    pfc_params p; size_t n_in = (size_t)w * h * elem(bd);
    size_t cap = pfc_bound(PFC_CODEC_IMAGE, n_in);
    uint8_t *enc = malloc(cap);
    void *dec = malloc(n_in);
    size_t out = 0, dout = 0;
    pfc_status se, sd;

    p.width = w; p.height = h; p.bitdepth = bd;
    se = pfc_encode(PFC_CODEC_IMAGE, &p, img, n_in, enc, cap, &out, g_work);
    CHECK(se == PFC_OK, name);
    CHECK(out <= cap, "output within pfc_bound (R5)");
    CHECK(out <= n_in + (n_in / 2u) + 1024u, "no pathological expansion");

    sd = pfc_decode(enc, out, dec, n_in, &dout, g_work);
    CHECK(sd == PFC_OK, "decode status OK");
    CHECK(dout == n_in, "decoded size matches");
    CHECK(memcmp(img, dec, n_in) == 0, "lossless round-trip (R1)");

    if (expect_small) {
        CHECK(out < n_in, "smooth data actually compressed");
        printf("    %s: %u x %u @%u-bit  %zu -> %zu  (%.2fx)\n",
               name, w, h, bd, n_in, out, (double)n_in / (double)out);
    } else {
        CHECK(out <= n_in + (n_in / 2u) + 1024u, "incompressible stays bounded (store-raw)");
        printf("    %s: %u x %u @%u-bit  %zu -> %zu  (store-raw path)\n",
               name, w, h, bd, n_in, out);
    }
    free(enc); free(dec); free(img);
}

static void test_fault_injection(void)
{
    uint32_t w = 200u, h = 120u; uint8_t bd = 16u;
    size_t n_in = (size_t)w * h * 2u;
    pfc_params p; p.width = w; p.height = h; p.bitdepth = bd;
    void *img = make_gradient(w, h, bd);
    size_t cap = pfc_bound(PFC_CODEC_IMAGE, n_in);
    uint8_t *enc = malloc(cap);
    uint16_t *dec = malloc(n_in);
    size_t out = 0, dout = 0;
    pfc_status sd;

    pfc_encode(PFC_CODEC_IMAGE, &p, img, n_in, enc, cap, &out, g_work);

    /* Flip a bit in band 0's payload (first block starts at header(20)+blockhdr(9)). */
    enc[20u + 9u + 4u] ^= 0x40u;

    sd = pfc_decode(enc, out, dec, n_in, &dout, g_work);
    CHECK(sd == PFC_E_CORRUPT, "corrupt block detected via CRC (R6)");
    CHECK(dout == n_in, "dst still fully written after corruption (containment)");

    /* Bands beyond band 0 (rows >= PFC_BAND_ROWS) must be intact. */
    {
        size_t base = (size_t)PFC_BAND_ROWS * w;
        size_t rest = (n_in / 2u) - base;
        CHECK(memcmp((uint16_t *)img + base, dec + base, rest * 2u) == 0,
              "undamaged bands decode correctly (error contained to one block)");
    }
    free(enc); free(dec); free(img);
}

static void test_truncation(void)
{
    uint32_t w = 64u, h = 64u; uint8_t bd = 8u;
    size_t n_in = (size_t)w * h;
    pfc_params p; p.width = w; p.height = h; p.bitdepth = bd;
    void *img = make_gradient(w, h, bd);
    size_t cap = pfc_bound(PFC_CODEC_IMAGE, n_in);
    uint8_t *enc = malloc(cap);
    uint8_t *dec = malloc(n_in);
    size_t out = 0, dout = 0;
    pfc_status sd;

    pfc_encode(PFC_CODEC_IMAGE, &p, img, n_in, enc, cap, &out, g_work);
    sd = pfc_decode(enc, out - 7u, dec, n_in, &dout, g_work);   /* chop mid-stream */
    CHECK(sd == PFC_E_CORRUPT, "truncated stream reported, no crash/OOB (R6)");
    CHECK(dout == n_in, "dst fully written on truncation");
    free(enc); free(dec); free(img);
}

int main(void)
{
    printf("libpfc test suite  (workmem = %zu bytes)\n", pfc_workmem_bytes());
    g_work = malloc(pfc_workmem_bytes());

    roundtrip("gradient16", 256u, 256u, 16u, make_gradient(256u, 256u, 16u), 1);
    roundtrip("gradient8",  320u, 200u, 8u,  make_gradient(320u, 200u, 8u), 1);
    roundtrip("odd-size16", 201u, 133u, 16u, make_gradient(201u, 133u, 16u), 1);
    roundtrip("width1",     1u,   500u, 16u, make_gradient(1u, 500u, 16u), 1);
    roundtrip("random16",   128u, 128u, 16u, make_random(128u, 128u, 16u, 12345u), 0);
    roundtrip("random8",    100u, 100u, 8u,  make_random(100u, 100u, 8u, 999u), 0);
    test_fault_injection();
    test_truncation();

    free(g_work);
    printf("\n%d passed, %d failed\n", g_pass, g_fail);
    return (g_fail == 0) ? 0 : 1;
}
