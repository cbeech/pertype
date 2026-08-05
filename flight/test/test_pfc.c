/* test_pfc.c — host test suite for libpfc.
 * SPDX-License-Identifier: Apache-2.0
 *
 * Covers: R1 round-trip (8/16-bit, odd sizes), R5 no-expansion + store-raw, R6 error containment
 * (bit-flip + truncation), and a compression-ratio sanity check on smooth data.
 */
#include "pfc.h"
#include "pfc_internal.h"
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

/* Constant background + flat objects + sparse spikes -> exercises run mode (exact-flat runs). */
static void *make_flat(uint32_t w, uint32_t h, uint8_t bd, uint32_t seed)
{
    size_t n = (size_t)w * h;
    uint32_t mask = (bd > 8u) ? 0xFFFFu : 0xFFu;
    uint32_t s = seed ? seed : 3u;
    size_t i;
    void *p = malloc(n * elem(bd));
    uint32_t bg = 1000u & mask;
    for (i = 0; i < n; i++) {
        uint32_t x = (uint32_t)(i % w), y = (uint32_t)(i / w);
        uint32_t v = ((y > (h / 4u)) && (y < (h / 2u)) && (x > 5u) && (x < (w - 5u))) ? (5000u & mask) : bg;
        if (bd > 8u) { ((uint16_t *)p)[i] = (uint16_t)v; } else { ((uint8_t *)p)[i] = (uint8_t)v; }
    }
    for (i = 0; i < (n / 200u); i++) {            /* sparse spikes (run interruptions) */
        s = s * 1664525u + 1013904223u;
        if (bd > 8u) { ((uint16_t *)p)[s % n] = (uint16_t)((s >> 8) & mask); }
        else { ((uint8_t *)p)[s % n] = (uint8_t)((s >> 8) & mask); }
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
    CHECK(out <= pfc_bound(PFC_CODEC_IMAGE, n_in), "no pathological expansion");

    sd = pfc_decode(enc, out, dec, n_in, &dout, g_work);
    CHECK(sd == PFC_OK, "decode status OK");
    CHECK(dout == n_in, "decoded size matches");
    CHECK(memcmp(img, dec, n_in) == 0, "lossless round-trip (R1)");

    if (expect_small) {
        CHECK(out < n_in, "smooth data actually compressed");
        printf("    %s: %u x %u @%u-bit  %zu -> %zu  (%.2fx)\n",
               name, w, h, bd, n_in, out, (double)n_in / (double)out);
    } else {
        CHECK(out <= pfc_bound(PFC_CODEC_IMAGE, n_in), "incompressible stays bounded (store-raw)");
        printf("    %s: %u x %u @%u-bit  %zu -> %zu  (store-raw path)\n",
               name, w, h, bd, n_in, out);
    }
    free(enc); free(dec); free(img);
}

static void check_rt(const char *name, pfc_codec codec, pfc_params *p,
                     const void *src, size_t n_in, int expect_small)
{
    size_t cap = pfc_bound(codec, n_in);
    uint8_t *enc = malloc(cap);
    void *dec = malloc(n_in);
    size_t out = 0, dout = 0;
    pfc_status se = pfc_encode(codec, p, src, n_in, enc, cap, &out, g_work);
    pfc_status sd;
    CHECK(se == PFC_OK, name);
    CHECK(out <= cap, "within pfc_bound");
    sd = pfc_decode(enc, out, dec, n_in, &dout, g_work);
    CHECK(sd == PFC_OK, "decode OK");
    CHECK(dout == n_in, "size matches");
    CHECK(memcmp(src, dec, n_in) == 0, "lossless round-trip (R1)");
    if (expect_small) { CHECK(out < n_in, "compressed"); }
    printf("    %s: %zu -> %zu  (%.2fx)%s\n", name, n_in, out, (double)n_in / (double)out,
           expect_small ? "" : "  [store-raw]");
    free(enc); free(dec);
}

static void test_seq(void)
{
    uint32_t n = 20000u;
    int16_t *ramp = malloc(n * sizeof(int16_t));
    int32_t *rnd = malloc(n * sizeof(int32_t));
    pfc_params p; uint32_t i; uint32_t s = 7u;
    memset(&p, 0, sizeof p);
    for (i = 0; i < n; i++) { ramp[i] = (int16_t)((int)(i % 600u) - 300); }   /* smooth */
    for (i = 0; i < n; i++) { s = s * 1664525u + 1013904223u; rnd[i] = (int32_t)s; }
    p.count = n; p.elem = 2; p.is_signed = 1;
    check_rt("seq-int16-ramp", PFC_CODEC_SEQ, &p, ramp, n * 2u, 1);
    p.count = n; p.elem = 4; p.is_signed = 1;
    check_rt("seq-int32-random", PFC_CODEC_SEQ, &p, rnd, n * 4u, 0);
    free(ramp); free(rnd);
}

static void test_float(void)
{
    uint32_t n = 8000u;
    float *sm = malloc(n * sizeof(float));
    pfc_params p; uint32_t i;
    memset(&p, 0, sizeof p);
    for (i = 0; i < n; i++) { sm[i] = (float)(i % 500u) * 0.25f; }   /* shared exponents */
    p.count = n; p.elem = 4;
    check_rt("float32-smooth", PFC_CODEC_FLOAT, &p, sm, n * 4u, 1);
    free(sm);
}

static void test_columnar(void)
{
    uint32_t n = 12000u, rw = 6u;          /* record = u32 ascending counter + u16 low-card flag */
    uint8_t *recs = malloc((size_t)n * rw);
    pfc_params p; uint32_t i;
    memset(&p, 0, sizeof p);
    for (i = 0; i < n; i++) {
        uint32_t ctr = i * 4u + 1000u;
        uint16_t flag = (uint16_t)(i % 3u);
        recs[i * rw + 0] = (uint8_t)(ctr & 0xFFu);
        recs[i * rw + 1] = (uint8_t)((ctr >> 8) & 0xFFu);
        recs[i * rw + 2] = (uint8_t)((ctr >> 16) & 0xFFu);
        recs[i * rw + 3] = (uint8_t)((ctr >> 24) & 0xFFu);
        recs[i * rw + 4] = (uint8_t)(flag & 0xFFu);
        recs[i * rw + 5] = (uint8_t)((flag >> 8) & 0xFFu);
    }
    p.width = rw; p.count = n;
    check_rt("columnar-records", PFC_CODEC_COLUMNAR, &p, recs, (size_t)n * rw, 1);
    free(recs);
}

/* Spectrally-correlated cube: each band is the previous one shifted + small spatial variation. */
static void *make_cube(uint32_t w, uint32_t h, uint32_t z, uint8_t bd)
{
    size_t n = (size_t)w * h * z;
    uint32_t mask = (bd > 8u) ? 0xFFFFu : 0xFFu;
    size_t i;
    void *p = malloc(n * elem(bd));
    for (i = 0; i < n; i++) {
        uint32_t b = (uint32_t)(i / ((size_t)w * h));
        uint32_t r = (uint32_t)(i % ((size_t)w * h));
        uint32_t x = r % w, y = r / w;
        uint32_t v = ((x * 5u + y * 3u + b * 40u) + ((x + y + b) & 7u)) & mask;  /* spectral + spatial */
        if (bd > 8u) { ((uint16_t *)p)[i] = (uint16_t)v; } else { ((uint8_t *)p)[i] = (uint8_t)v; }
    }
    return p;
}

static void test_spectral(void)
{
    pfc_params p;
    memset(&p, 0, sizeof p); p.width = 120; p.height = 100; p.count = 32; p.bitdepth = 16;
    check_rt("spectral-cube16", PFC_CODEC_SPECTRAL, &p, make_cube(120, 100, 32, 16),
             (size_t)120 * 100 * 32 * 2, 1);
    memset(&p, 0, sizeof p); p.width = 64; p.height = 48; p.count = 16; p.bitdepth = 8;
    check_rt("spectral-cube8", PFC_CODEC_SPECTRAL, &p, make_cube(64, 48, 16, 8),
             (size_t)64 * 48 * 16, 1);
    memset(&p, 0, sizeof p); p.width = 40; p.height = 40; p.count = 3; p.bitdepth = 16;  /* odd/small */
    check_rt("spectral-small", PFC_CODEC_SPECTRAL, &p, make_cube(40, 40, 3, 16),
             (size_t)40 * 40 * 3 * 2, 1);
}

static void test_spectral_corruption(void)
{
    pfc_params q;
    size_t n, cap, out = 0, dout = 0, i;
    uint8_t *src, *enc, *e2, *dec, buf[64], d2[64];
    memset(&q, 0, sizeof q); q.width = 40; q.height = 40; q.count = 8; q.bitdepth = 16;
    n = (size_t)40 * 40 * 8 * 2; cap = pfc_bound(PFC_CODEC_SPECTRAL, n);
    src = malloc(n); for (i = 0; i < n; i++) { src[i] = (uint8_t)(i & 0x3Fu); }
    enc = malloc(cap); dec = malloc(n);
    pfc_encode(PFC_CODEC_SPECTRAL, &q, src, n, enc, cap, &out, g_work);
    e2 = malloc(out); memcpy(e2, enc, out);
    e2[24u + 9u + 1u] ^= 0x55u;                       /* flip in block 0 payload (24-byte header) */
    CHECK((pfc_decode(e2, out, dec, n, &dout, g_work) == PFC_E_CORRUPT) && (dout == n),
          "spectral corruption contained");
    CHECK((pfc_decode(enc, out - 4u, dec, n, &dout, g_work) == PFC_E_CORRUPT) && (dout == n),
          "spectral truncation contained");
    free(src); free(enc); free(e2); free(dec);
    /* validation: bad bitdepth, src_len mismatch, malformed decode header */
    for (i = 0; i < 64u; i++) { buf[i] = (uint8_t)i; }
    memset(&q, 0, sizeof q); q.width = 4; q.height = 4; q.count = 1; q.bitdepth = 7;
    CHECK(pfc_encode(PFC_CODEC_SPECTRAL, &q, buf, 32, d2, 64, &out, g_work) == PFC_E_PARAM,
          "spectral bad bitdepth");
    q.bitdepth = 16;
    CHECK(pfc_encode(PFC_CODEC_SPECTRAL, &q, buf, 31, d2, 64, &out, g_work) == PFC_E_PARAM,
          "spectral src_len mismatch");
    {
        uint8_t s[28]; memset(s, 0, sizeof s); s[0] = 'P'; s[1] = 'F'; s[2] = 'C'; s[3] = '1';
        s[4] = 1; s[5] = 5; s[6] = 16; s[8] = 0;     /* width 0 -> corrupt */
        CHECK(pfc_decode(s, 28, d2, 64, &out, g_work) == PFC_E_CORRUPT, "spectral decode bad dims");
    }
    {
        /* Regression for a real libFuzzer-found heap-buffer-overflow: a 20-23 byte input passes
         * the top-level pfc_decode() dispatcher's generic PFC_HDR(20)-byte check, but SPECTRAL's
         * real header is PFC_SPEC_HDR(24) bytes -- pfc_spectral_decode used to read s[20..23]
         * unconditionally, one byte past a 20-byte buffer. Mirrors the exact crashing input. */
        uint8_t s[20] = {'P','F','C','1', 1, 5, 0xff,0x01,0xff,0x21,0xff,0x00,0xff,0xfb,0x0a,0xf5,0x01,0x95,0x50,0x43};
        CHECK(pfc_decode(s, sizeof s, d2, 64, &out, g_work) == PFC_E_CORRUPT,
              "spectral truncated-header (20B) rejected, not OOB-read");
    }
    {
        /* Regression for a real bug found extending CBMC coverage: width*height*count*es is up
         * to 78 bits of untrusted wire-header input -- width=2, es=2 (bd=16), height=count=2^31
         * makes the true product EXACTLY 2^64, which a naive 64-bit `(size_t)width*height*count*
         * es` computes as 0 (full wraparound) -- so the old check `cap < total` was `cap < 0`,
         * always false, meaning ANY cap (even 0) was accepted as "big enough" for a stream that
         * actually needs 2^64 bytes. See pfc_size_mul in pfc_internal.h. */
        uint8_t s[24];
        memset(s, 0, sizeof s);
        s[0] = 'P'; s[1] = 'F'; s[2] = 'C'; s[3] = '1'; s[4] = 1; s[5] = (uint8_t)PFC_CODEC_SPECTRAL;
        s[6] = 16;                                   /* bd=16 -> es=2 */
        s[8] = 2;                                     /* width = 2 (LE) */
        s[12] = 0; s[13] = 0; s[14] = 0; s[15] = 0x80; /* height = 2^31 (LE) */
        s[16] = 0; s[17] = 0; s[18] = 0; s[19] = 0x80; /* count = 2^31 (LE) */
        s[20] = 1;                                     /* band = 1 (LE) */
        CHECK(pfc_decode(s, sizeof s, d2, 64, &out, g_work) == PFC_E_BOUND,
              "spectral width*height*count*es overflow (2^64) rejected, not silently wrapped");
    }
}

static void test_columnar_oversized_block(void)
{
    /* Regression for a real libFuzzer-found heap-buffer-overflow: unlike the encoder (which always
     * derives block_recs = PFC_BLOCK_BYTES / rw, so rw*block_recs never exceeds the fixed xform
     * scratch buffer), the decoder read block_recs straight from the untrusted stream header with
     * no upper bound. rw=2, block_recs=cnt=40000 -> nr*rw=80000 > PFC_BLOCK_BYTES(65536), which
     * used to write w->xform[65536] (UBSan: index out of bounds; ASan: heap-buffer-overflow,
     * pfc_columnar.c:150). No block-record payload is needed: the check fires purely from the
     * 20-byte header, before pfc_block_read is ever attempted -- PROVIDED cap is large enough to
     * pass the earlier `cap < rw*cnt` (80000) guard first and actually reach it; a too-small cap
     * (as an earlier version of this test mistakenly used) returns PFC_E_BOUND there instead. */
    uint8_t s[20]; size_t out = 0; size_t cap = 90000u;
    uint8_t *d2 = malloc(cap);
    memset(s, 0, sizeof s);
    s[0] = 'P'; s[1] = 'F'; s[2] = 'C'; s[3] = '1';
    s[4] = 1; s[5] = (uint8_t)PFC_CODEC_COLUMNAR;
    s[8] = 2u;                                  /* rw = 2 */
    s[12] = 0x40u; s[13] = 0x9Cu;                /* cnt = 40000 (LE) */
    s[16] = 0x40u; s[17] = 0x9Cu;                /* block_recs = 40000 (LE) */
    CHECK(pfc_decode(s, sizeof s, d2, cap, &out, g_work) == PFC_E_CORRUPT,
          "columnar oversized block_recs rejected, not OOB-write");
    free(d2);
}

static void test_block_read_bounds(void)
{
    /* Locks in the exact boundary of pfc_block_read's rewritten (subtraction-based) length
     * check, guarding against a regression back to the addition-based form (p+PFC_BLKHDR+n>len)
     * that could wrap size_t on a 32-bit target -- see pfc_frame.c and proofs/cbmc/. This host
     * test only proves the boundary is correct under the host's own (64-bit) size_t; it cannot
     * exercise the wraparound itself, which is what the CBMC proof (--32) is for. */
    uint8_t buf[20];
    size_t pos;
    const uint8_t *payload;
    size_t plen;
    uint8_t flags;
    uint32_t crc;

    /* rem = len(20) - pos(0) - PFC_BLKHDR(9) = 11: exact-fit payload_len must be accepted. */
    memset(buf, 0, sizeof buf);
    crc = pfc_crc32(&buf[9], 11u);
    pfc_put_u32(&buf[0], 11u);
    pfc_put_u32(&buf[5], crc);
    pos = 0u;
    CHECK(pfc_block_read(buf, sizeof buf, &pos, &payload, &plen, &flags) == PFC_OK,
          "block_read accepts exact-fit payload_len (rem boundary)");
    CHECK(pos == sizeof buf, "block_read advances pos to end of buffer on exact fit");

    /* rem+1 (12) must be rejected without advancing pos, not silently truncated/wrapped. */
    pfc_put_u32(&buf[0], 12u);
    pos = 0u;
    CHECK(pfc_block_read(buf, sizeof buf, &pos, &payload, &plen, &flags) == PFC_E_CORRUPT,
          "block_read rejects payload_len one past the boundary");
    CHECK(pos == 0u, "block_read leaves pos unchanged on rejection");
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

/* Parameter validation + malformed-stream rejection across all codecs (no crash, right status). */
static void test_validation(void)
{
    uint8_t buf[256], dst[256], e[512]; size_t out = 0, o2 = 0; pfc_params p; uint32_t i;
    for (i = 0; i < 256u; i++) { buf[i] = (uint8_t)i; }
    memset(&p, 0, sizeof p);
    p.width = 8; p.height = 8; p.bitdepth = 8;
    CHECK(pfc_encode(PFC_CODEC_IMAGE, &p, buf, 63, dst, 256, &out, g_work) == PFC_E_PARAM, "image src_len mismatch");
    p.bitdepth = 10;
    CHECK(pfc_encode(PFC_CODEC_IMAGE, &p, buf, 64, dst, 256, &out, g_work) == PFC_E_PARAM, "image bad bitdepth");
    memset(&p, 0, sizeof p); p.count = 8; p.elem = 3;
    CHECK(pfc_encode(PFC_CODEC_SEQ, &p, buf, 24, dst, 256, &out, g_work) == PFC_E_PARAM, "seq bad elem");
    p.elem = 2;
    CHECK(pfc_encode(PFC_CODEC_SEQ, &p, buf, 15, dst, 256, &out, g_work) == PFC_E_PARAM, "seq src_len mismatch");
    memset(&p, 0, sizeof p); p.count = 4; p.elem = 2;
    CHECK(pfc_encode(PFC_CODEC_FLOAT, &p, buf, 8, dst, 256, &out, g_work) == PFC_E_PARAM, "float bad elem");
    p.elem = 4;
    CHECK(pfc_encode(PFC_CODEC_FLOAT, &p, buf, 15, dst, 256, &out, g_work) == PFC_E_PARAM, "float src_len mismatch");
    memset(&p, 0, sizeof p); p.width = 0; p.count = 4;
    CHECK(pfc_encode(PFC_CODEC_COLUMNAR, &p, buf, 0, dst, 256, &out, g_work) == PFC_E_PARAM, "columnar width 0");
    p.width = 4; p.count = 4;
    CHECK(pfc_encode(PFC_CODEC_COLUMNAR, &p, buf, 15, dst, 256, &out, g_work) == PFC_E_PARAM, "columnar src_len mismatch");
    CHECK(pfc_encode((pfc_codec)9, &p, buf, 16, dst, 256, &out, g_work) == PFC_E_UNSUPPORTED, "encode unsupported codec");
    p.width = 8; p.height = 8; p.bitdepth = 8;
    CHECK(pfc_encode(PFC_CODEC_IMAGE, &p, buf, 64, dst, 5, &out, g_work) == PFC_E_BOUND, "encode tiny cap");

    {   /* malformed decode inputs */
        uint8_t s[40]; memset(s, 0, sizeof s);
        CHECK(pfc_decode(s, 40, dst, 256, &out, g_work) == PFC_E_CORRUPT, "decode bad magic");
        s[0] = 'P'; s[1] = 'F'; s[2] = 'C'; s[3] = '1'; s[4] = 2;
        CHECK(pfc_decode(s, 40, dst, 256, &out, g_work) == PFC_E_UNSUPPORTED, "decode bad version");
        s[4] = 1; s[5] = 9;
        CHECK(pfc_decode(s, 40, dst, 256, &out, g_work) == PFC_E_UNSUPPORTED, "decode unsupported codec");
        CHECK(pfc_decode(s, 3, dst, 256, &out, g_work) == PFC_E_CORRUPT, "decode too short");
    }
    pfc_encode(PFC_CODEC_IMAGE, &p, buf, 64, e, 512, &o2, g_work);
    CHECK(pfc_decode(e, o2, dst, 1, &out, g_work) == PFC_E_BOUND, "decode dst too small");
}

/* Corruption containment for a given codec: bit-flip a block payload, then truncate. */
static void corrupt_codec(const char *name, pfc_codec codec, pfc_params p, size_t n_in)
{
    uint8_t *src = malloc(n_in), *enc, *e2, *dec = malloc(n_in);
    size_t cap = pfc_bound(codec, n_in), out = 0, dout = 0, i;
    pfc_status s1, s2;
    for (i = 0; i < n_in; i++) { src[i] = (uint8_t)(i & 0x3Fu); }  /* low-entropy -> coded */
    enc = malloc(cap);
    pfc_encode(codec, &p, src, n_in, enc, cap, &out, g_work);
    e2 = malloc(out); memcpy(e2, enc, out);
    e2[20u + 9u + 1u] ^= 0x55u;                      /* flip a byte in block 0's payload */
    s1 = pfc_decode(e2, out, dec, n_in, &dout, g_work);
    CHECK((s1 == PFC_E_CORRUPT) && (dout == n_in), name);
    s2 = pfc_decode(enc, out - 4u, dec, n_in, &dout, g_work);    /* truncate tail */
    CHECK((s2 == PFC_E_CORRUPT) && (dout == n_in), "truncation contained");
    free(src); free(enc); free(e2); free(dec);
}

static void test_corruption_all(void)
{
    pfc_params p;
    memset(&p, 0, sizeof p); p.count = 4000; p.elem = 2; p.is_signed = 1;
    corrupt_codec("seq corruption contained", PFC_CODEC_SEQ, p, 8000);
    memset(&p, 0, sizeof p); p.count = 3000; p.elem = 4;
    corrupt_codec("float corruption contained", PFC_CODEC_FLOAT, p, 12000);
    memset(&p, 0, sizeof p); p.width = 6; p.count = 3000;
    corrupt_codec("columnar corruption contained", PFC_CODEC_COLUMNAR, p, 18000);
    {   /* image early truncation: only header survives -> all bands filled (multi-band repair) */
        uint8_t *src = make_gradient(80u, 80u, 16u); pfc_params q;
        size_t n = 80u * 80u * 2u, cap = pfc_bound(PFC_CODEC_IMAGE, n), out = 0, dout = 0;
        uint8_t *enc = malloc(cap), *dec = malloc(n);
        q.width = 80; q.height = 80; q.bitdepth = 16;
        pfc_encode(PFC_CODEC_IMAGE, &q, src, n, enc, cap, &out, g_work);
        CHECK(pfc_decode(enc, 22u, dec, n, &dout, g_work) == PFC_E_CORRUPT, "image early-truncation contained");
        CHECK(dout == n, "image early-truncation fully written");
        free(src); free(enc); free(dec);
    }
}

/* Remaining defensive guards: internal count/cap checks and malformed per-codec decode headers. */
static void test_more_guards(void)
{
    uint8_t buf[256], dst[256], e[512]; size_t out = 0, o2 = 0; pfc_params p; uint32_t i;
    for (i = 0; i < 256u; i++) { buf[i] = (uint8_t)i; }
    /* encode internal guards: count==0 and header-won't-fit cap, per codec */
    memset(&p, 0, sizeof p); p.count = 0; p.elem = 2;
    CHECK(pfc_encode(PFC_CODEC_SEQ, &p, buf, 0, dst, 256, &out, g_work) == PFC_E_PARAM, "seq count 0");
    memset(&p, 0, sizeof p); p.width = 4; p.count = 0;
    CHECK(pfc_encode(PFC_CODEC_COLUMNAR, &p, buf, 0, dst, 256, &out, g_work) == PFC_E_PARAM, "columnar count 0");
    memset(&p, 0, sizeof p); p.count = 10; p.elem = 2;
    CHECK(pfc_encode(PFC_CODEC_SEQ, &p, buf, 20, dst, 5, &out, g_work) == PFC_E_BOUND, "seq tiny cap");
    memset(&p, 0, sizeof p); p.width = 2; p.count = 10;
    CHECK(pfc_encode(PFC_CODEC_COLUMNAR, &p, buf, 20, dst, 5, &out, g_work) == PFC_E_BOUND, "columnar tiny cap");
    /* malformed per-codec decode headers (valid magic/ver, bad params) */
    {
        uint8_t s[40]; memset(s, 0, sizeof s); s[0] = 'P'; s[1] = 'F'; s[2] = 'C'; s[3] = '1'; s[4] = 1;
        s[5] = 1; s[6] = 11; s[8] = 8; s[12] = 8; s[16] = 16;        /* IMAGE bad bitdepth */
        CHECK(pfc_decode(s, 40, dst, 256, &out, g_work) == PFC_E_CORRUPT, "decode image bad bitdepth");
        s[6] = 8; s[8] = 0;                                          /* IMAGE width 0 */
        CHECK(pfc_decode(s, 40, dst, 256, &out, g_work) == PFC_E_CORRUPT, "decode image width 0");
        memset(s, 0, sizeof s); s[0] = 'P'; s[1] = 'F'; s[2] = 'C'; s[3] = '1'; s[4] = 1;
        s[5] = 2; s[6] = 3; s[8] = 4; s[12] = 16;                    /* SEQ bad elem */
        CHECK(pfc_decode(s, 40, dst, 256, &out, g_work) == PFC_E_CORRUPT, "decode seq bad elem");
    }
    /* valid streams decoded into an undersized dst -> E_BOUND (seq + columnar paths) */
    memset(&p, 0, sizeof p); p.count = 10; p.elem = 2;
    pfc_encode(PFC_CODEC_SEQ, &p, buf, 20, e, 512, &o2, g_work);
    CHECK(pfc_decode(e, o2, dst, 1, &out, g_work) == PFC_E_BOUND, "decode seq dst too small");
    memset(&p, 0, sizeof p); p.width = 2; p.count = 10;
    pfc_encode(PFC_CODEC_COLUMNAR, &p, buf, 20, e, 512, &o2, g_work);
    CHECK(pfc_decode(e, o2, dst, 1, &out, g_work) == PFC_E_BOUND, "decode columnar dst too small");
}

/* Regression for a real fault-tolerance defect found by SEU fault injection (test/seu_inject.c):
 * a single flipped bit that zeroes a freq[][] entry made pfc_rc_encode compute range = 0, after
 * which pfc_rc_enc_renorm's exit condition could never become false -- an INFINITE LOOP, i.e. a
 * hung encoder on the spacecraft, strictly worse than the silent corruption 2.5 anticipated. It
 * was also a latent JPL Power-of-Ten Rule 2 violation (unbounded loop). Fixed by bounding both
 * renorm loops with PFC_RC_RENORM_MAX; the encoder now reports overflow, which every codec already
 * handles by falling back to store-raw.
 *
 * freq >= 1 is an invariant on uncorrupted state, so this is only reachable via memory corruption
 * -- which is precisely the flight fault model. The test therefore corrupts the model directly
 * rather than waiting for a random upset to find it. It must TERMINATE; that is the assertion. */
static void test_renorm_bound(void)
{
    pfc_rc_enc e;
    uint8_t out[256];
    size_t i;

    pfc_model_reset(g_work);
    /* Zero every frequency in context 0: the strongest form of the corruption, guaranteeing
     * range==0 on the next encode into that context regardless of which symbol is selected. */
    for (i = 0u; i < PFC_NSYM; i++) {
        g_work->freq[0][i] = 0u;
    }
    pfc_rc_enc_init(&e, out, sizeof out);
    pfc_resid_encode(&e, g_work, 0u, 5);   /* hung forever before the fix */

    CHECK(e.overflow == 1, "renorm bound: zeroed model reports overflow instead of hanging");

    /* And the healthy path must be untouched -- no early bail-out on good state. */
    pfc_model_reset(g_work);
    pfc_rc_enc_init(&e, out, sizeof out);
    pfc_resid_encode(&e, g_work, 0u, 5);
    CHECK(e.overflow == 0, "renorm bound: healthy model still encodes without overflow");
}

/* SPECTRAL inter-band refresh bands (pfc_params::elem, 0 = off). Two properties matter and both
 * are load-bearing:
 *   (a) refresh=0 must be BYTE-IDENTICAL to the pre-feature encoder, or the feature silently
 *       changed every existing caller's output;
 *   (b) every interval must still round-trip losslessly (R1) -- a containment feature that breaks
 *       losslessness would be a far worse bug than the propagation it fixes.
 * The containment benefit itself is measured separately (test/downlink_containment.c). */
static void test_spectral_refresh(void)
{
    const uint32_t W = 16u, H = 16u, Z = 8u;
    size_t n_in = (size_t)W * H * Z * 2u;
    size_t cap = pfc_bound(PFC_CODEC_SPECTRAL, n_in);
    uint8_t *src = malloc(n_in), *dec = malloc(n_in);
    uint8_t *enc = malloc(cap), *ref = malloc(cap);
    size_t ref_len = 0;
    const uint8_t intervals[4] = { 0u, 2u, 3u, 4u };
    uint32_t s = 7u;
    size_t i;
    int k;

    for (i = 0u; i < (n_in / 2u); i++) {
        s = s * 1664525u + 1013904223u;
        ((uint16_t *)src)[i] = (uint16_t)(((i % 61u) * 17u) + ((s >> 26) & 3u));
    }

    for (k = 0; k < 4; k++) {
        pfc_params p;
        size_t enc_len = 0, dout = 0;
        memset(&p, 0, sizeof p);
        p.width = W; p.height = H; p.count = Z; p.bitdepth = 16u;
        p.elem = intervals[k];
        CHECK(pfc_encode(PFC_CODEC_SPECTRAL, &p, src, n_in, enc, cap, &enc_len, g_work) == PFC_OK,
              "spectral refresh: encode OK");
        memset(dec, 0, n_in);
        CHECK(pfc_decode(enc, enc_len, dec, n_in, &dout, g_work) == PFC_OK,
              "spectral refresh: decode OK");
        CHECK(memcmp(src, dec, n_in) == 0, "spectral refresh: lossless round-trip (R1)");
        if (k == 0) { memcpy(ref, enc, enc_len); ref_len = enc_len; }
    }

    /* Re-encode with refresh=0 and demand the exact original bytes back. */
    {
        pfc_params p;
        size_t enc_len = 0;
        memset(&p, 0, sizeof p);
        p.width = W; p.height = H; p.count = Z; p.bitdepth = 16u; p.elem = 0u;
        CHECK(pfc_encode(PFC_CODEC_SPECTRAL, &p, src, n_in, enc, cap, &enc_len, g_work) == PFC_OK,
              "spectral refresh: baseline re-encode OK");
        CHECK((enc_len == ref_len) && (memcmp(enc, ref, enc_len) == 0),
              "spectral refresh: refresh=0 is byte-identical (no silent change)");
    }

    free(src); free(dec); free(enc); free(ref);
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
    roundtrip("flat16(run)", 300u, 240u, 16u, make_flat(300u, 240u, 16u, 7u), 1);
    roundtrip("flat8(run)",  256u, 256u, 8u,  make_flat(256u, 256u, 8u, 11u), 1);
    test_seq();
    test_float();
    test_columnar();
    test_spectral();
    test_spectral_corruption();
    test_columnar_oversized_block();
    test_block_read_bounds();
    test_fault_injection();
    test_truncation();
    test_validation();
    test_corruption_all();
    test_more_guards();
    test_renorm_bound();
    test_spectral_refresh();

    free(g_work);
    printf("\n%d passed, %d failed\n", g_pass, g_fail);
    return (g_fail == 0) ? 0 : 1;
}
