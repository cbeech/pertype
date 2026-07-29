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

int main(void)
{
    printf("MC/DC-targeted tests (decision-structure, not just behaviour)\n\n");
    mcdc_encode_arg_guard();
    mcdc_decode_arg_guard();
    mcdc_decode_magic();
    mcdc_elem_guards();
    mcdc_version_and_codec();
    printf("\n%d passed, %d failed\n", g_pass, g_fail);
    return (g_fail == 0) ? 0 : 1;
}
