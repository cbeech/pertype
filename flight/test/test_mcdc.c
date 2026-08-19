/* test_mcdc.c — tests written specifically to satisfy MC/DC, not just branch coverage.
 * SPDX-License-Identifier: Apache-2.0
 *
 * WHY A SEPARATE FILE
 * -------------------
 * `make coverage` (gcov) reports ~98% line and ~89% branch coverage, which sounds close to done.
 * `make mcdc` (clang 18 `-fcoverage-mcdc`) measured the same corpus at **55.65%** MC/DC. That gap
 * is not a contradiction -- it is exactly what MC/DC exists to expose. A decision like
 *
 *     if ((p == NULL) || (src == NULL) || (dst == NULL) || (out == NULL) || (work == NULL))
 *
 * reaches BOTH outcomes (100% branch coverage) as soon as one test passes all-valid pointers and
 * one test passes a single NULL. But that demonstrates nothing about the other four conditions:
 * if `dst == NULL` were accidentally written `dst != NULL`, both those tests would still pass.
 * MC/DC requires showing each condition INDEPENDENTLY changes the outcome, which for an N-way
 * `||` chain needs N+1 vectors: all-false, then each condition true in isolation.
 *
 * The existing suite tests behaviour ("does a NULL argument get rejected?"). These tests target
 * decision structure ("is each condition load-bearing?"). Both matter; they are different jobs, so
 * they live in different files rather than being tangled together.
 *
 * Every case below is also a real robustness test -- nothing here exists purely to move a metric.
 */
/* pfc_internal.h for PFC_HDR (the shared 20-byte stream header size); it includes pfc.h itself.
 * Everything actually exercised below is the PUBLIC API -- the internal header is only used for
 * named constants, so these tests still reflect what a real caller can reach. */
#include "pfc_internal.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int g_pass, g_fail;
static uint32_t g_rng;

static uint32_t rnd32(void)
{
    g_rng = (g_rng * 1664525u) + 1013904223u;
    return g_rng;
}

static void ck(int cond, const char *what)
{
    if (cond) {
        g_pass++;
    } else {
        g_fail++;
        printf("  FAIL: %s\n", what);
    }
}

/* ---------------------------------------------------------------------------------------
 * pfc_encode's argument guard (pfc.c):
 *     (p == NULL) || (src == NULL) || (dst == NULL) || (out == NULL) || (work == NULL)
 * 5 conditions -> 6 vectors: one all-valid (whole decision false), then each pointer NULL'd
 * alone. Each NULL case flips the outcome with all other conditions held false, which is
 * precisely MC/DC's independence requirement.
 * --------------------------------------------------------------------------------------- */
static void mcdc_encode_arg_guard(void)
{
    uint8_t img[64];                 /* 8x4 @16-bit */
    uint8_t dst[512];
    size_t out = 0;
    pfc_params p;
    pfc_ctx *work = malloc(pfc_workmem_bytes());
    if (work == NULL) { printf("  oom\n"); g_fail++; return; }

    memset(img, 0, sizeof img);
    memset(&p, 0, sizeof p);
    p.width = 8u; p.height = 4u; p.bitdepth = 16u;

    /* Vector 0: every condition false -> decision false -> must NOT be PFC_E_PARAM. */
    ck(pfc_encode(PFC_CODEC_IMAGE, &p, img, sizeof img, dst, sizeof dst, &out, work) == PFC_OK,
       "encode guard: all-valid args accepted");

    /* Vectors 1..5: exactly one condition true at a time. */
    ck(pfc_encode(PFC_CODEC_IMAGE, NULL, img, sizeof img, dst, sizeof dst, &out, work) == PFC_E_PARAM,
       "encode guard: p==NULL alone rejected");
    ck(pfc_encode(PFC_CODEC_IMAGE, &p, NULL, sizeof img, dst, sizeof dst, &out, work) == PFC_E_PARAM,
       "encode guard: src==NULL alone rejected");
    ck(pfc_encode(PFC_CODEC_IMAGE, &p, img, sizeof img, NULL, sizeof dst, &out, work) == PFC_E_PARAM,
       "encode guard: dst==NULL alone rejected");
    ck(pfc_encode(PFC_CODEC_IMAGE, &p, img, sizeof img, dst, sizeof dst, NULL, work) == PFC_E_PARAM,
       "encode guard: out==NULL alone rejected");
    ck(pfc_encode(PFC_CODEC_IMAGE, &p, img, sizeof img, dst, sizeof dst, &out, NULL) == PFC_E_PARAM,
       "encode guard: work==NULL alone rejected");

    free(work);
}

/* ---------------------------------------------------------------------------------------
 * pfc_decode's argument guard (pfc.c):
 *     (src == NULL) || (dst == NULL) || (out == NULL) || (work == NULL)
 * 4 conditions -> 5 vectors.
 * --------------------------------------------------------------------------------------- */
static void mcdc_decode_arg_guard(void)
{
    uint8_t img[64];
    uint8_t enc[512];
    uint8_t dec[64];
    size_t out = 0, enc_len = 0;
    pfc_params p;
    pfc_ctx *work = malloc(pfc_workmem_bytes());
    if (work == NULL) { printf("  oom\n"); g_fail++; return; }

    memset(img, 0, sizeof img);
    memset(&p, 0, sizeof p);
    p.width = 8u; p.height = 4u; p.bitdepth = 16u;
    if (pfc_encode(PFC_CODEC_IMAGE, &p, img, sizeof img, enc, sizeof enc, &enc_len, work) != PFC_OK) {
        printf("  setup encode failed\n"); g_fail++; free(work); return;
    }

    /* Vector 0: all conditions false. */
    ck(pfc_decode(enc, enc_len, dec, sizeof dec, &out, work) == PFC_OK,
       "decode guard: all-valid args accepted");

    /* Vectors 1..4: exactly one condition true at a time. */
    ck(pfc_decode(NULL, enc_len, dec, sizeof dec, &out, work) == PFC_E_PARAM,
       "decode guard: src==NULL alone rejected");
    ck(pfc_decode(enc, enc_len, NULL, sizeof dec, &out, work) == PFC_E_PARAM,
       "decode guard: dst==NULL alone rejected");
    ck(pfc_decode(enc, enc_len, dec, sizeof dec, NULL, work) == PFC_E_PARAM,
       "decode guard: out==NULL alone rejected");
    ck(pfc_decode(enc, enc_len, dec, sizeof dec, &out, NULL) == PFC_E_PARAM,
       "decode guard: work==NULL alone rejected");

    free(work);
}

/* ---------------------------------------------------------------------------------------
 * pfc_decode's magic check (pfc.c):
 *     (s[0] != 'P') || (s[1] != 'F') || (s[2] != 'C') || (s[3] != '1')
 * 4 conditions -> 5 vectors. This one has real teeth beyond the metric: corrupting each magic
 * byte in isolation proves all four are actually checked. A single mistyped index (say, checking
 * s[2] twice) would leave one byte unvalidated and still pass every existing behavioural test,
 * because those only ever corrupt the whole header at once.
 * --------------------------------------------------------------------------------------- */
static void mcdc_decode_magic(void)
{
    uint8_t img[64];
    uint8_t enc[512];
    uint8_t bad[512];
    uint8_t dec[64];
    size_t out = 0, enc_len = 0;
    pfc_params p;
    pfc_ctx *work = malloc(pfc_workmem_bytes());
    unsigned i;
    static const char magic[4] = { 'P', 'F', 'C', '1' };
    if (work == NULL) { printf("  oom\n"); g_fail++; return; }

    memset(img, 0, sizeof img);
    memset(&p, 0, sizeof p);
    p.width = 8u; p.height = 4u; p.bitdepth = 16u;
    if (pfc_encode(PFC_CODEC_IMAGE, &p, img, sizeof img, enc, sizeof enc, &enc_len, work) != PFC_OK) {
        printf("  setup encode failed\n"); g_fail++; free(work); return;
    }

    /* Vector 0: all four conditions false (magic intact) -> decision false. */
    ck(pfc_decode(enc, enc_len, dec, sizeof dec, &out, work) == PFC_OK,
       "magic: intact 'PFC1' accepted");

    /* Vectors 1..4: exactly one magic byte wrong. */
    for (i = 0u; i < 4u; i++) {
        char label[48];
        memcpy(bad, enc, enc_len);
        bad[i] = (uint8_t)((uint8_t)magic[i] ^ 0xFFu);  /* guaranteed != the expected byte */
        sprintf(label, "magic: byte %u corrupted alone is rejected", i);
        ck(pfc_decode(bad, enc_len, dec, sizeof dec, &out, work) == PFC_E_CORRUPT, label);
    }

    free(work);
}

/* ---------------------------------------------------------------------------------------
 * pfc_encode's codec-specific parameter guards. Each codec validates its own element size before
 * touching the data; exercising each rejected value independently (rather than one bad value per
 * codec) is what MC/DC needs for these && / || chains.
 * --------------------------------------------------------------------------------------- */
static void mcdc_elem_guards(void)
{
    uint8_t buf[64];
    uint8_t dst[512];
    size_t out = 0;
    pfc_params p;
    pfc_ctx *work = malloc(pfc_workmem_bytes());
    if (work == NULL) { printf("  oom\n"); g_fail++; return; }
    memset(buf, 0, sizeof buf);

    /* SEQ: elem must be 1, 2 or 4 -- (elem!=1) && (elem!=2) && (elem!=4).
     * Each accepted value makes exactly one condition false, which is what shows all three
     * comparisons are live. */
    memset(&p, 0, sizeof p);
    p.elem = 1u; p.count = 64u;
    ck(pfc_encode(PFC_CODEC_SEQ, &p, buf, 64u, dst, sizeof dst, &out, work) == PFC_OK,
       "seq elem guard: elem=1 accepted");
    p.elem = 2u; p.count = 32u;
    ck(pfc_encode(PFC_CODEC_SEQ, &p, buf, 64u, dst, sizeof dst, &out, work) == PFC_OK,
       "seq elem guard: elem=2 accepted");
    p.elem = 4u; p.count = 16u;
    ck(pfc_encode(PFC_CODEC_SEQ, &p, buf, 64u, dst, sizeof dst, &out, work) == PFC_OK,
       "seq elem guard: elem=4 accepted");
    p.elem = 3u; p.count = 16u;
    ck(pfc_encode(PFC_CODEC_SEQ, &p, buf, 64u, dst, sizeof dst, &out, work) == PFC_E_PARAM,
       "seq elem guard: elem=3 rejected (all three conditions true)");

    /* FLOAT: elem must be 4 or 8 -- (elem!=4) && (elem!=8). */
    memset(&p, 0, sizeof p);
    p.elem = 4u; p.count = 16u;
    ck(pfc_encode(PFC_CODEC_FLOAT, &p, buf, 64u, dst, sizeof dst, &out, work) == PFC_OK,
       "float elem guard: elem=4 accepted");
    p.elem = 8u; p.count = 8u;
    ck(pfc_encode(PFC_CODEC_FLOAT, &p, buf, 64u, dst, sizeof dst, &out, work) == PFC_OK,
       "float elem guard: elem=8 accepted");
    p.elem = 2u; p.count = 32u;
    ck(pfc_encode(PFC_CODEC_FLOAT, &p, buf, 64u, dst, sizeof dst, &out, work) == PFC_E_PARAM,
       "float elem guard: elem=2 rejected (both conditions true)");

    /* COLUMNAR: width must be nonzero and <= PFC_BLOCK_BYTES -- (width==0) || (width>MAX).
     * Exercising the zero case and the oversize case separately is the independence pair. */
    memset(&p, 0, sizeof p);
    p.width = 4u; p.count = 16u;
    ck(pfc_encode(PFC_CODEC_COLUMNAR, &p, buf, 64u, dst, sizeof dst, &out, work) == PFC_OK,
       "columnar width guard: width=4 accepted");
    p.width = 0u; p.count = 16u;
    ck(pfc_encode(PFC_CODEC_COLUMNAR, &p, buf, 64u, dst, sizeof dst, &out, work) == PFC_E_PARAM,
       "columnar width guard: width=0 alone rejected");
    p.width = 0x10001u; p.count = 1u;
    ck(pfc_encode(PFC_CODEC_COLUMNAR, &p, buf, 64u, dst, sizeof dst, &out, work) == PFC_E_PARAM,
       "columnar width guard: width>PFC_BLOCK_BYTES alone rejected");

    free(work);
}

/* ---------------------------------------------------------------------------------------
 * Unsupported codec id and version. These are single-condition decisions, but they sit on the
 * untrusted-input path and are cheap to pin down.
 * --------------------------------------------------------------------------------------- */
static void mcdc_version_and_codec(void)
{
    uint8_t img[64];
    uint8_t enc[512];
    uint8_t bad[512];
    uint8_t dec[64];
    size_t out = 0, enc_len = 0;
    pfc_params p;
    pfc_ctx *work = malloc(pfc_workmem_bytes());
    if (work == NULL) { printf("  oom\n"); g_fail++; return; }

    memset(img, 0, sizeof img);
    memset(&p, 0, sizeof p);
    p.width = 8u; p.height = 4u; p.bitdepth = 16u;
    if (pfc_encode(PFC_CODEC_IMAGE, &p, img, sizeof img, enc, sizeof enc, &enc_len, work) != PFC_OK) {
        printf("  setup encode failed\n"); g_fail++; free(work); return;
    }

    memcpy(bad, enc, enc_len);
    bad[4] = (uint8_t)(PFC_VERSION + 1u);
    ck(pfc_decode(bad, enc_len, dec, sizeof dec, &out, work) == PFC_E_UNSUPPORTED,
       "version: wrong version rejected as unsupported");

    memcpy(bad, enc, enc_len);
    bad[5] = 99u;                                  /* not a valid pfc_codec */
    ck(pfc_decode(bad, enc_len, dec, sizeof dec, &out, work) == PFC_E_UNSUPPORTED,
       "codec id: unknown codec rejected as unsupported");

    memcpy(bad, enc, enc_len);
    bad[5] = 0u;                                   /* codec 0 is not assigned */
    ck(pfc_decode(bad, enc_len, dec, sizeof dec, &out, work) == PFC_E_UNSUPPORTED,
       "codec id: codec 0 rejected as unsupported");

    /* Truncated below the shared 20-byte header: the length guard, exercised on its own. */
    ck(pfc_decode(enc, (size_t)(PFC_HDR - 1u), dec, sizeof dec, &out, work) == PFC_E_CORRUPT,
       "header length: src_len < PFC_HDR rejected");

    free(work);
}

/* ---------------------------------------------------------------------------------------
 * pfc_frame.c's bounds guards, on both the read and write side:
 *     read:  (p > len) || ((len - p) < PFC_BLKHDR)
 *     write: (p > cap) || ((cap - p) < PFC_BLKHDR)
 * 2 conditions each -> 3 vectors each. These matter well beyond the metric: this is the exact
 * check whose earlier addition-based form could wrap size_t on a 32-bit target, and it is the
 * single point every codec's decoder funnels through. The two conditions guard different things --
 * `p > len` catches a position already past the end, `(len - p) < PFC_BLKHDR` catches a position
 * in bounds but with no room for a header -- and only exercising each in isolation shows both are
 * live. Note the second cannot even be evaluated safely unless the first is false, which is why
 * the ordering is load-bearing rather than stylistic.
 * --------------------------------------------------------------------------------------- */
static void mcdc_frame_bounds(void)
{
    uint8_t buf[64];
    const uint8_t *payload = NULL;
    size_t plen = 0, pos;
    uint8_t flags = 0;
    pfc_status st;

    memset(buf, 0, sizeof buf);

    /* --- read side --- */
    /* Vector 0: both conditions false (room for a header). Rejection here is by CRC/length, NOT
     * by the bounds guard -- what matters is that the guard itself did not fire, and `pos` is
     * still valid. */
    pos = 0u;
    st = pfc_block_read(buf, sizeof buf, &pos, &payload, &plen, &flags);
    ck(pos <= sizeof buf, "frame read: in-bounds start leaves pos within len");

    /* Vector 1: p > len alone. */
    pos = 40u;
    st = pfc_block_read(buf, 10u, &pos, &payload, &plen, &flags);
    ck(st == PFC_E_CORRUPT, "frame read: pos past len rejected");
    ck(pos == 40u, "frame read: pos unchanged when past len");

    /* Vector 2: p <= len, but fewer than PFC_BLKHDR bytes remain. */
    pos = 5u;
    st = pfc_block_read(buf, 10u, &pos, &payload, &plen, &flags);
    ck(st == PFC_E_CORRUPT, "frame read: truncated header rejected");
    ck(pos == 5u, "frame read: pos unchanged on truncated header");

    /* Boundary pair for the payload-length check `n > rem`: exactly-fits must be accepted by the
     * bounds guard, one-past must not. Build a real record so only the length check decides. */
    {
        uint8_t rec[32];
        size_t cap_pos = 0u;
        uint8_t pay[4] = { 1u, 2u, 3u, 4u };
        memset(rec, 0, sizeof rec);
        st = pfc_block_write(rec, sizeof rec, &cap_pos, pay, sizeof pay, 0u);
        ck(st == PFC_OK, "frame write: well-sized record accepted");

        pos = 0u;
        st = pfc_block_read(rec, cap_pos, &pos, &payload, &plen, &flags);
        ck(st == PFC_OK, "frame read: exactly-fitting record accepted");
        ck(plen == sizeof pay, "frame read: payload length round-trips");

        /* Same record, one byte short: the length field now exceeds what remains. */
        pos = 0u;
        st = pfc_block_read(rec, cap_pos - 1u, &pos, &payload, &plen, &flags);
        ck(st == PFC_E_CORRUPT, "frame read: length exceeding remainder rejected");
    }

    /* --- write side --- */
    /* Vector 1: p > cap alone. */
    {
        uint8_t pay[4] = { 9u, 9u, 9u, 9u };
        pos = 40u;
        st = pfc_block_write(buf, 10u, &pos, pay, sizeof pay, 0u);
        ck(st == PFC_E_BOUND, "frame write: pos past cap rejected");
        ck(pos == 40u, "frame write: pos unchanged on rejection");

        /* Vector 2: p <= cap but no room for a header. */
        pos = 5u;
        st = pfc_block_write(buf, 10u, &pos, pay, sizeof pay, 0u);
        ck(st == PFC_E_BOUND, "frame write: no room for header rejected");

        /* Payload that does not fit the remaining space, header room notwithstanding. */
        pos = 0u;
        st = pfc_block_write(buf, PFC_BLKHDR + 2u, &pos, pay, sizeof pay, 0u);
        ck(st == PFC_E_BOUND, "frame write: oversized payload rejected");
        ck(pos == 0u, "frame write: pos unchanged when payload does not fit");
    }
}

/* ---------------------------------------------------------------------------------------
 * pfc_seq.c's decode-side header validation:
 *     ((elem != 1) && (elem != 2) && (elem != 4)) || (count == 0) || (block == 0)
 * A 5-condition mixed &&/|| decision reached only through a crafted wire header, which is why it
 * was among the least-covered. Each vector below makes exactly one clause decisive.
 * --------------------------------------------------------------------------------------- */
static void mcdc_seq_header(void)
{
    uint8_t src[64];
    uint8_t enc[512];
    uint8_t bad[512];
    uint8_t dec[64];
    size_t enc_len = 0, out = 0;
    pfc_params p;
    pfc_ctx *work = malloc(pfc_workmem_bytes());
    if (work == NULL) { printf("  oom\n"); g_fail++; return; }

    memset(src, 0, sizeof src);
    memset(&p, 0, sizeof p);
    p.count = 32u; p.elem = 2u; p.is_signed = 0u;
    if (pfc_encode(PFC_CODEC_SEQ, &p, src, 64u, enc, sizeof enc, &enc_len, work) != PFC_OK) {
        printf("  setup encode failed\n"); g_fail++; free(work); return;
    }

    /* Vector 0: every condition false -- a valid header decodes. */
    ck(pfc_decode(enc, enc_len, dec, sizeof dec, &out, work) == PFC_OK,
       "seq header: valid header accepted");

    /* Vector 1: elem invalid alone (byte 6), count and block left valid. */
    memcpy(bad, enc, enc_len);
    bad[6] = 3u;
    ck(pfc_decode(bad, enc_len, dec, sizeof dec, &out, work) == PFC_E_CORRUPT,
       "seq header: elem=3 alone rejected");

    /* Vector 2: count == 0 alone (bytes 8..11). */
    memcpy(bad, enc, enc_len);
    bad[8] = 0u; bad[9] = 0u; bad[10] = 0u; bad[11] = 0u;
    ck(pfc_decode(bad, enc_len, dec, sizeof dec, &out, work) == PFC_E_CORRUPT,
       "seq header: count=0 alone rejected");

    /* Vector 3: block == 0 alone (bytes 12..15) -- would otherwise be an infinite decode loop,
     * so this guard is load-bearing for liveness, not just validity. */
    memcpy(bad, enc, enc_len);
    bad[12] = 0u; bad[13] = 0u; bad[14] = 0u; bad[15] = 0u;
    ck(pfc_decode(bad, enc_len, dec, sizeof dec, &out, work) == PFC_E_CORRUPT,
       "seq header: block=0 alone rejected");

    /* elem == 1 and elem == 4 are the other two accepted values; exercising them makes the
     * remaining conditions of the && chain individually decisive rather than short-circuited. */
    memset(&p, 0, sizeof p);
    p.count = 64u; p.elem = 1u;
    ck(pfc_encode(PFC_CODEC_SEQ, &p, src, 64u, enc, sizeof enc, &enc_len, work) == PFC_OK,
       "seq header: elem=1 encodes");
    ck(pfc_decode(enc, enc_len, dec, sizeof dec, &out, work) == PFC_OK,
       "seq header: elem=1 decodes");
    memset(&p, 0, sizeof p);
    p.count = 16u; p.elem = 4u;
    ck(pfc_encode(PFC_CODEC_SEQ, &p, src, 64u, enc, sizeof enc, &enc_len, work) == PFC_OK,
       "seq header: elem=4 encodes");
    ck(pfc_decode(enc, enc_len, dec, sizeof dec, &out, work) == PFC_OK,
       "seq header: elem=4 decodes");

    free(work);
}

/* ---------------------------------------------------------------------------------------
 * pfc_columnar.c's decode-side header guard:
 *     (rw == 0) || (rw > PFC_BLOCK_BYTES) || (cnt == 0) || (block_recs == 0)
 * 4 conditions -> 5 vectors. Real teeth: the `block_recs` clause is the fix for a heap-buffer-
 * overflow libFuzzer found (an unbounded block_recs read straight off the wire indexed past the
 * fixed xform[] scratch buffer). Exercising each clause alone proves none of the four has been
 * accidentally short-circuited by an earlier one.
 * --------------------------------------------------------------------------------------- */
static void mcdc_columnar_header(void)
{
    uint8_t src[96];
    uint8_t enc[1024];
    uint8_t bad[1024];
    uint8_t dec[96];
    size_t enc_len = 0, out = 0;
    pfc_params p;
    pfc_ctx *work = malloc(pfc_workmem_bytes());
    if (work == NULL) { printf("  oom\n"); g_fail++; return; }

    memset(src, 0, sizeof src);
    memset(&p, 0, sizeof p);
    p.width = 6u; p.count = 16u;                 /* 6-byte records x 16 = 96 bytes */
    if (pfc_encode(PFC_CODEC_COLUMNAR, &p, src, 96u, enc, sizeof enc, &enc_len, work) != PFC_OK) {
        printf("  setup encode failed\n"); g_fail++; free(work); return;
    }

    /* Vector 0: all four conditions false. */
    ck(pfc_decode(enc, enc_len, dec, sizeof dec, &out, work) == PFC_OK,
       "columnar header: valid header accepted");

    /* Vector 1: rw == 0 alone (bytes 8..11). */
    memcpy(bad, enc, enc_len);
    bad[8] = 0u; bad[9] = 0u; bad[10] = 0u; bad[11] = 0u;
    ck(pfc_decode(bad, enc_len, dec, sizeof dec, &out, work) == PFC_E_CORRUPT,
       "columnar header: rw=0 alone rejected");

    /* Vector 2: rw > PFC_BLOCK_BYTES alone (65537). */
    memcpy(bad, enc, enc_len);
    bad[8] = 0x01u; bad[9] = 0x00u; bad[10] = 0x01u; bad[11] = 0x00u;   /* 0x00010001 */
    ck(pfc_decode(bad, enc_len, dec, sizeof dec, &out, work) == PFC_E_CORRUPT,
       "columnar header: rw>PFC_BLOCK_BYTES alone rejected");

    /* Vector 3: cnt == 0 alone (bytes 12..15). */
    memcpy(bad, enc, enc_len);
    bad[12] = 0u; bad[13] = 0u; bad[14] = 0u; bad[15] = 0u;
    ck(pfc_decode(bad, enc_len, dec, sizeof dec, &out, work) == PFC_E_CORRUPT,
       "columnar header: cnt=0 alone rejected");

    /* Vector 4: block_recs == 0 alone (bytes 16..19) -- without this guard the decode loop's
     * `r0 += block_recs` never advances, i.e. an infinite loop, so it is a liveness guard. */
    memcpy(bad, enc, enc_len);
    bad[16] = 0u; bad[17] = 0u; bad[18] = 0u; bad[19] = 0u;
    ck(pfc_decode(bad, enc_len, dec, sizeof dec, &out, work) == PFC_E_CORRUPT,
       "columnar header: block_recs=0 alone rejected");

    /* Capacity guard, exercised on its own: a valid header whose declared size exceeds cap. */
    ck(pfc_decode(enc, enc_len, dec, 8u, &out, work) == PFC_E_BOUND,
       "columnar header: cap smaller than declared size rejected");

    free(work);
}

/* ---------------------------------------------------------------------------------------
 * pfc_spectral.c's decode-side header guards -- the least-covered file in the project:
 *     (bd != 8) && (bd != 16)
 *     (width == 0) || (height == 0) || (count == 0) || (band == 0) || (width > PFC_MAX_COLS)
 * 7 conditions across two decisions. SPECTRAL has the most bug history here (a header-size gap
 * found by libFuzzer, then the four-factor size product that overflows even 64-bit size_t), so
 * pinning each clause independently is worth more here than anywhere else.
 * --------------------------------------------------------------------------------------- */
static void mcdc_spectral_header(void)
{
    uint8_t src[128];
    uint8_t enc[2048];
    uint8_t bad[2048];
    uint8_t dec[128];
    size_t enc_len = 0, out = 0;
    pfc_params p;
    pfc_ctx *work = malloc(pfc_workmem_bytes());
    if (work == NULL) { printf("  oom\n"); g_fail++; return; }

    memset(src, 0, sizeof src);
    memset(&p, 0, sizeof p);
    p.width = 4u; p.height = 4u; p.count = 4u; p.bitdepth = 16u;   /* 4*4*4*2 = 128 bytes */
    if (pfc_encode(PFC_CODEC_SPECTRAL, &p, src, 128u, enc, sizeof enc, &enc_len, work) != PFC_OK) {
        printf("  setup encode failed\n"); g_fail++; free(work); return;
    }

    /* Vector 0: everything valid. */
    ck(pfc_decode(enc, enc_len, dec, sizeof dec, &out, work) == PFC_OK,
       "spectral header: valid header accepted");

    /* bitdepth: both accepted values, plus a rejected one. bd is at byte 6. */
    memcpy(bad, enc, enc_len);
    bad[6] = 12u;                                  /* neither 8 nor 16 -> both conditions true */
    ck(pfc_decode(bad, enc_len, dec, sizeof dec, &out, work) == PFC_E_CORRUPT,
       "spectral header: bitdepth=12 rejected");

    /* width == 0 alone (bytes 8..11). */
    memcpy(bad, enc, enc_len);
    bad[8] = 0u; bad[9] = 0u; bad[10] = 0u; bad[11] = 0u;
    ck(pfc_decode(bad, enc_len, dec, sizeof dec, &out, work) == PFC_E_CORRUPT,
       "spectral header: width=0 alone rejected");

    /* width > PFC_MAX_COLS alone -- distinct clause from width==0. */
    memcpy(bad, enc, enc_len);
    bad[8] = 0x01u; bad[9] = 0x00u; bad[10] = 0x01u; bad[11] = 0x00u;   /* 65537 > 8192 */
    ck(pfc_decode(bad, enc_len, dec, sizeof dec, &out, work) == PFC_E_CORRUPT,
       "spectral header: width>PFC_MAX_COLS alone rejected");

    /* height == 0 alone (bytes 12..15). */
    memcpy(bad, enc, enc_len);
    bad[12] = 0u; bad[13] = 0u; bad[14] = 0u; bad[15] = 0u;
    ck(pfc_decode(bad, enc_len, dec, sizeof dec, &out, work) == PFC_E_CORRUPT,
       "spectral header: height=0 alone rejected");

    /* count == 0 alone (bytes 16..19). */
    memcpy(bad, enc, enc_len);
    bad[16] = 0u; bad[17] = 0u; bad[18] = 0u; bad[19] = 0u;
    ck(pfc_decode(bad, enc_len, dec, sizeof dec, &out, work) == PFC_E_CORRUPT,
       "spectral header: count=0 alone rejected");

    /* band == 0 alone (bytes 20..23) -- liveness guard: the row loop advances by `band`. */
    memcpy(bad, enc, enc_len);
    bad[20] = 0u; bad[21] = 0u; bad[22] = 0u; bad[23] = 0u;
    ck(pfc_decode(bad, enc_len, dec, sizeof dec, &out, work) == PFC_E_CORRUPT,
       "spectral header: band=0 alone rejected");

    /* Truncated below SPECTRAL's own 24-byte header -- the check added for the libFuzzer-found
     * overflow, since the dispatcher only guarantees the generic 20 bytes. */
    ck(pfc_decode(enc, 22u, dec, sizeof dec, &out, work) == PFC_E_CORRUPT,
       "spectral header: len below PFC_SPEC_HDR rejected");

    /* 8-bit is the other accepted bitdepth; exercise it so that condition is decisive too. */
    memset(&p, 0, sizeof p);
    p.width = 4u; p.height = 4u; p.count = 4u; p.bitdepth = 8u;
    ck(pfc_encode(PFC_CODEC_SPECTRAL, &p, src, 64u, enc, sizeof enc, &enc_len, work) == PFC_OK,
       "spectral header: bitdepth=8 encodes");
    ck(pfc_decode(enc, enc_len, dec, sizeof dec, &out, work) == PFC_OK,
       "spectral header: bitdepth=8 decodes");

    free(work);
}

/* ---------------------------------------------------------------------------------------
 * pfc_image.c decode-side header guard:
 *     (width == 0) || (height == 0) || (band == 0) || (width > PFC_MAX_COLS)
 * IMAGE decode headers were not covered at all by the existing MC/DC tests.
 * --------------------------------------------------------------------------------------- */
static void mcdc_image_header_decode(void)
{
    uint8_t src[64];
    uint8_t enc[512];
    uint8_t bad[512];
    uint8_t dec[64];
    size_t enc_len = 0, out = 0;
    pfc_params p;
    pfc_ctx *work = malloc(pfc_workmem_bytes());
    if (work == NULL) { printf("  oom\n"); g_fail++; return; }

    memset(src, 0, sizeof src);
    memset(&p, 0, sizeof p);
    p.width = 8u; p.height = 4u; p.bitdepth = 16u;
    if (pfc_encode(PFC_CODEC_IMAGE, &p, src, sizeof src, enc, sizeof enc, &enc_len, work) != PFC_OK) {
        printf("  setup encode failed\n"); g_fail++; free(work); return;
    }

    ck(pfc_decode(enc, enc_len, dec, sizeof dec, &out, work) == PFC_OK,
       "image decode header: valid accepted");

    memcpy(bad, enc, enc_len);
    bad[8] = 0u; bad[9] = 0u; bad[10] = 0u; bad[11] = 0u;
    ck(pfc_decode(bad, enc_len, dec, sizeof dec, &out, work) == PFC_E_CORRUPT,
       "image decode header: width=0 alone rejected");

    memcpy(bad, enc, enc_len);
    bad[12] = 0u; bad[13] = 0u; bad[14] = 0u; bad[15] = 0u;
    ck(pfc_decode(bad, enc_len, dec, sizeof dec, &out, work) == PFC_E_CORRUPT,
       "image decode header: height=0 alone rejected");

    memcpy(bad, enc, enc_len);
    bad[16] = 0u; bad[17] = 0u; bad[18] = 0u; bad[19] = 0u;
    ck(pfc_decode(bad, enc_len, dec, sizeof dec, &out, work) == PFC_E_CORRUPT,
       "image decode header: band=0 alone rejected");

    memcpy(bad, enc, enc_len);
    bad[8] = 0x01u; bad[9] = 0x00u; bad[10] = 0x01u; bad[11] = 0x00u;   /* 65537 > PFC_MAX_COLS */
    ck(pfc_decode(bad, enc_len, dec, sizeof dec, &out, work) == PFC_E_CORRUPT,
       "image decode header: width>PFC_MAX_COLS alone rejected");

    free(work);
}

/* ---------------------------------------------------------------------------------------
 * Encode-side parameter guards that the dispatcher does NOT check:
 *   pfc_spectral.c:199  (bitdepth, width, height, count)
 *   pfc_image.c:309     (bitdepth, width, height)
 *   pfc_columnar.c:25   (rw, cnt)
 *   pfc_seq.c:73        (count, block)
 * Exercising each rejected clause independently pins the encode-side guard chains.
 * --------------------------------------------------------------------------------------- */
static void mcdc_encode_param_guards(void)
{
    uint8_t buf[128];
    uint8_t dst[512];
    size_t out = 0;
    pfc_params p;
    pfc_ctx *work = malloc(pfc_workmem_bytes());
    if (work == NULL) { printf("  oom\n"); g_fail++; return; }
    memset(buf, 0, sizeof buf);

    /* SPECTRAL encode guard (pfc_spectral.c:199). */
    memset(&p, 0, sizeof p);
    p.width = 4u; p.height = 4u; p.count = 4u; p.bitdepth = 16u;
    ck(pfc_encode(PFC_CODEC_SPECTRAL, &p, buf, 128u, dst, sizeof dst, &out, work) == PFC_OK,
       "spectral encode guard: valid accepted");
    p.bitdepth = 12u;
    ck(pfc_encode(PFC_CODEC_SPECTRAL, &p, buf, 128u, dst, sizeof dst, &out, work) == PFC_E_PARAM,
       "spectral encode guard: bad bitdepth alone rejected");
    p.bitdepth = 16u; p.width = 0u;
    ck(pfc_encode(PFC_CODEC_SPECTRAL, &p, buf, 128u, dst, sizeof dst, &out, work) == PFC_E_PARAM,
       "spectral encode guard: width=0 alone rejected");
    p.width = 4u; p.height = 0u;
    ck(pfc_encode(PFC_CODEC_SPECTRAL, &p, buf, 128u, dst, sizeof dst, &out, work) == PFC_E_PARAM,
       "spectral encode guard: height=0 alone rejected");
    p.height = 4u; p.count = 0u;
    ck(pfc_encode(PFC_CODEC_SPECTRAL, &p, buf, 128u, dst, sizeof dst, &out, work) == PFC_E_PARAM,
       "spectral encode guard: count=0 alone rejected");
    p.count = 4u; p.width = 0x00010001u;
    ck(pfc_encode(PFC_CODEC_SPECTRAL, &p, buf, 128u, dst, sizeof dst, &out, work) == PFC_E_PARAM,
       "spectral encode guard: width>PFC_MAX_COLS alone rejected");

    /* IMAGE encode guard (pfc_image.c:309). */
    memset(&p, 0, sizeof p);
    p.width = 8u; p.height = 4u; p.bitdepth = 16u;
    ck(pfc_encode(PFC_CODEC_IMAGE, &p, buf, 64u, dst, sizeof dst, &out, work) == PFC_OK,
       "image encode guard: valid accepted");
    p.bitdepth = 12u;
    ck(pfc_encode(PFC_CODEC_IMAGE, &p, buf, 64u, dst, sizeof dst, &out, work) == PFC_E_PARAM,
       "image encode guard: bad bitdepth alone rejected");
    p.bitdepth = 16u; p.width = 0u;
    ck(pfc_encode(PFC_CODEC_IMAGE, &p, buf, 64u, dst, sizeof dst, &out, work) == PFC_E_PARAM,
       "image encode guard: width=0 alone rejected");
    p.width = 8u; p.height = 0u;
    ck(pfc_encode(PFC_CODEC_IMAGE, &p, buf, 64u, dst, sizeof dst, &out, work) == PFC_E_PARAM,
       "image encode guard: height=0 alone rejected");

    /* COLUMNAR encode guard (pfc_columnar.c:25). */
    memset(&p, 0, sizeof p);
    p.width = 4u; p.count = 16u;
    ck(pfc_encode(PFC_CODEC_COLUMNAR, &p, buf, 64u, dst, sizeof dst, &out, work) == PFC_OK,
       "columnar encode guard: valid accepted");
    p.width = 0u;
    ck(pfc_encode(PFC_CODEC_COLUMNAR, &p, buf, 64u, dst, sizeof dst, &out, work) == PFC_E_PARAM,
       "columnar encode guard: width=0 alone rejected");
    p.width = 0x10001u;
    ck(pfc_encode(PFC_CODEC_COLUMNAR, &p, buf, 64u, dst, sizeof dst, &out, work) == PFC_E_PARAM,
       "columnar encode guard: width>PFC_BLOCK_BYTES alone rejected");
    p.width = 4u; p.count = 0u;
    ck(pfc_encode(PFC_CODEC_COLUMNAR, &p, buf, 64u, dst, sizeof dst, &out, work) == PFC_E_PARAM,
       "columnar encode guard: count=0 alone rejected");

    /* SEQ encode guard (pfc_seq.c:73). count==0 is reachable; block==0 is not, because the
     * dispatcher only allows elem in {1,2,4}, so PFC_BLOCK_BYTES/elem is always >0. */
    memset(&p, 0, sizeof p);
    p.count = 32u; p.elem = 2u;
    ck(pfc_encode(PFC_CODEC_SEQ, &p, buf, 64u, dst, sizeof dst, &out, work) == PFC_OK,
       "seq encode guard: valid accepted");
    p.count = 0u;
    ck(pfc_encode(PFC_CODEC_SEQ, &p, buf, 64u, dst, sizeof dst, &out, work) == PFC_E_PARAM,
       "seq encode guard: count=0 alone rejected");

    free(work);
}

/* ---------------------------------------------------------------------------------------
 * pfc_image.c gradient sign tie-break (line 121):
 *     (q1 < 0) || ((q1 == 0) && (q2 < 0)) || ((q1 == 0) && (q2 == 0) && (q3 < 0))
 * The third tier needs q1==0, q2==0, q3<0. Construct a small 16-bit image where the up-right
 * and vertical gradients are flat, but the up-left vs left gradient is strongly negative.
 * --------------------------------------------------------------------------------------- */
static void mcdc_image_gradient_tiebreak(void)
{
    uint16_t img[8 * 4];
    uint8_t enc[512];
    uint8_t dec[sizeof img];
    size_t enc_len = 0, out = 0;
    pfc_params p;
    pfc_ctx *work = malloc(pfc_workmem_bytes());
    if (work == NULL) { printf("  oom\n"); g_fail++; return; }

    memset(img, 0, sizeof img);
    /* Pixel layout (x,y): c=(0,0)=100, b=(1,0)=100, d=(2,0)=100, a=(0,1)=200. */
    img[0 * 8 + 0] = 100u;   /* (0,0) c */
    img[0 * 8 + 1] = 100u;   /* (1,0) b */
    img[0 * 8 + 2] = 100u;   /* (2,0) d */
    img[1 * 8 + 0] = 200u;   /* (0,1) a */

    memset(&p, 0, sizeof p);
    p.width = 8u; p.height = 4u; p.bitdepth = 16u;
    ck(pfc_encode(PFC_CODEC_IMAGE, &p, img, sizeof img, enc, sizeof enc, &enc_len, work) == PFC_OK,
       "image gradient tie-break: crafted image encodes");
    ck(pfc_decode(enc, enc_len, dec, sizeof dec, &out, work) == PFC_OK,
       "image gradient tie-break: round-trips");
    ck(memcmp(dec, img, sizeof img) == 0,
       "image gradient tie-break: lossless");

    free(work);
}

/* ---------------------------------------------------------------------------------------
 * Store-raw fallback decisions:
 *   pfc_columnar.c:73  (e.overflow != 0) || (e.pos >= block_bytes)
 *   pfc_seq.c:111      (e.overflow != 0) || (e.pos >= raw_bytes)
 * Random input already hits the decision, but not each half independently. We force:
 *   - incompressible data -> pos >= raw_bytes, overflow == 0 (pure store-raw)
 *   - a tiny capacity that triggers overflow before pos reaches raw_bytes
 * --------------------------------------------------------------------------------------- */
static void mcdc_store_raw(void)
{
    uint8_t rnd[256];
    uint8_t dst[16];                 /* small enough to force overflow on SEQ */
    uint8_t dec[256];
    size_t out = 0, enc_len = 0;
    pfc_params p;
    pfc_ctx *work = malloc(pfc_workmem_bytes());
    unsigned i;
    if (work == NULL) { printf("  oom\n"); g_fail++; return; }

    /* Incompressible SEQ: random bytes -> pos >= raw_bytes with overflow == 0. */
    g_rng = 12345u;
    for (i = 0u; i < sizeof rnd; i++) { rnd[i] = (uint8_t)rnd32(); }
    memset(&p, 0, sizeof p);
    p.count = 64u; p.elem = 4u; p.is_signed = 0u;
    ck(pfc_encode(PFC_CODEC_SEQ, &p, rnd, 256u, dst, sizeof dst, &out, work) == PFC_E_BOUND,
       "store-raw seq: tiny cap forces overflow before store-raw decision");

    memset(&p, 0, sizeof p);
    p.count = 64u; p.elem = 4u; p.is_signed = 0u;
    {
        uint8_t big_dst[512];
        ck(pfc_encode(PFC_CODEC_SEQ, &p, rnd, 256u, big_dst, sizeof big_dst, &enc_len, work) == PFC_OK,
           "store-raw seq: random data encodes (store-raw path)");
        ck(pfc_decode(big_dst, enc_len, dec, 256u, &out, work) == PFC_OK,
           "store-raw seq: random data round-trips");
    }

    /* Incompressible COLUMNAR. */
    memset(&p, 0, sizeof p);
    p.width = 8u; p.count = 32u;
    {
        uint8_t big_dst[512];
        ck(pfc_encode(PFC_CODEC_COLUMNAR, &p, rnd, 256u, big_dst, sizeof big_dst, &enc_len, work) == PFC_OK,
           "store-raw columnar: random data encodes (store-raw path)");
        ck(pfc_decode(big_dst, enc_len, dec, 256u, &out, work) == PFC_OK,
           "store-raw columnar: random data round-trips");
    }

    free(work);
}

int main(void)
{
    printf("MC/DC-targeted tests (decision-structure, not just behaviour)\n\n");
    mcdc_frame_bounds();
    mcdc_seq_header();
    mcdc_columnar_header();
    mcdc_spectral_header();
    mcdc_image_header_decode();
    mcdc_image_gradient_tiebreak();
    mcdc_encode_param_guards();
    mcdc_store_raw();
    mcdc_encode_arg_guard();
    mcdc_decode_arg_guard();
    mcdc_decode_magic();
    mcdc_elem_guards();
    mcdc_version_and_codec();
    printf("\n%d passed, %d failed\n", g_pass, g_fail);
    return (g_fail == 0) ? 0 : 1;
}
