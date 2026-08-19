/* seu_detect.c — would a cheap model self-check convert SILENT SEU corruption into DETECTED?
 * SPDX-License-Identifier: Apache-2.0
 *
 * THE PROBLEM (measured in test/seu_inject.c)
 * ------------------------------------------
 * Zero of 7200 encoder-side single-bit upsets were detected by anything. The CRC is computed AFTER
 * the corruption, over the already-wrong payload, so a corrupted block is self-consistent and
 * passes on the ground. Encoder-side SEU is a wholly silent failure mode, and ~3 KB of adaptive
 * model state (0.91% of the 330 KB workmem) carries essentially all the risk.
 *
 * mission-safety.md 2.5 recommends checksumming that state. But a checksum needs somewhere to
 * store the expected value and a policy for when to recompute it, because the model MUTATES
 * constantly (it is adaptive). There is a cheaper option the code already gives us for free:
 *
 * THE INVARIANT
 * -------------
 *     tot[ctx] == sum(freq[ctx][s] for all s)
 *
 * pfc_model.c maintains this everywhere: reset sets both consistently, update adds PFC_MODEL_INC
 * to one entry AND to tot, rescale recomputes both. So it is a genuine redundancy already present
 * in the data structure -- checking it costs ZERO extra storage, just a recomputed sum. A single
 * flipped bit anywhere in freq[][] or tot[] breaks it.
 *
 * WHAT THIS MEASURES
 * ------------------
 * For each injected upset: does the invariant catch it, and would that have mattered? Cross-tabs
 * detection against outcome, because catching corruptions that were harmless anyway is worth
 * nothing -- the figure that matters is **what fraction of the SILENT (dangerous) corruptions the
 * check converts into DETECTED**.
 *
 * The check runs per block, hooked via the linker's --wrap on pfc_model_reset (called at the start
 * of every block), so it verifies the state the block just finished with -- exactly where a real
 * implementation would put it. src/ is compiled unmodified.
 *
 * Known scope limit, stated rather than discovered later: the invariant covers freq[][] and tot[]
 * (2520 of the 3012 model bytes). It does NOT cover mant[][] (132 B) or bias_*[] (360 B), which
 * have no equivalent redundancy -- those would still need a real checksum. The measurement below
 * reports that residual explicitly.
 */
#include "pfc_internal.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

void __real_pfc_resid_encode(pfc_rc_enc *e, pfc_ctx *w, unsigned ctx, int32_t resid);
void __real_pfc_model_reset(pfc_ctx *w);

static long     g_call_count, g_inject_at;
static size_t   g_inject_byte;
static unsigned g_inject_bit;
static pfc_ctx *g_live;              /* the ctx being encoded, for the per-block check */
static int      g_violation_seen;    /* invariant broken at any block boundary this trial */

static int invariant_holds(const pfc_ctx *w)
{
    unsigned c, s;
    for (c = 0u; c < PFC_NCTX; c++) {
        uint32_t sum = 0u;
        for (s = 0u; s < PFC_NSYM; s++) { sum += w->freq[c][s]; }
        if (sum != w->tot[c]) { return 0; }
    }
    return 1;
}

void __wrap_pfc_model_reset(pfc_ctx *w)
{
    /* Verify the state the PREVIOUS block finished with, before wiping it. */
    if ((g_live != NULL) && (!invariant_holds(w))) { g_violation_seen = 1; }
    g_live = w;
    __real_pfc_model_reset(w);
}

void __wrap_pfc_resid_encode(pfc_rc_enc *e, pfc_ctx *w, unsigned ctx, int32_t resid)
{
    if ((g_inject_at >= 0) && (g_call_count == g_inject_at)) {
        ((uint8_t *)w)[g_inject_byte] ^= (uint8_t)(1u << g_inject_bit);
    }
    g_call_count++;
    __real_pfc_resid_encode(e, w, ctx, resid);
}

static uint32_t g_rng;
static uint32_t rnd(void) { g_rng = (g_rng * 1664525u) + 1013904223u; return g_rng >> 8; }

typedef enum { REG_FREQ, REG_TOT, REG_MANT, REG_BIAS, REG_SCRATCH, REG_XFORM, REG_N } region;
static const char *rname[REG_N] = { "freq[][]", "tot[]", "mant[][]", "bias_*[]",
                                    "scratch[]", "xform[]" };
static size_t rs[REG_N], re[REG_N];

static void bounds_init(void)
{
    struct pfc_ctx p;
    size_t b = (size_t)(uintptr_t)&p;
    rs[REG_FREQ]=(size_t)(uintptr_t)&p.freq-b;    rs[REG_TOT]=(size_t)(uintptr_t)&p.tot-b;
    rs[REG_MANT]=(size_t)(uintptr_t)&p.mant-b;    rs[REG_BIAS]=(size_t)(uintptr_t)&p.bias_c-b;
    rs[REG_SCRATCH]=(size_t)(uintptr_t)&p.scratch-b; rs[REG_XFORM]=(size_t)(uintptr_t)&p.xform-b;
    re[REG_FREQ]=rs[REG_TOT]; re[REG_TOT]=rs[REG_MANT]; re[REG_MANT]=rs[REG_BIAS];
    re[REG_BIAS]=rs[REG_SCRATCH]; re[REG_SCRATCH]=rs[REG_XFORM]; re[REG_XFORM]=sizeof(struct pfc_ctx);
}

int main(int argc, char **argv)
{
    const uint32_t W = 64u, H = 64u;
    long per_region = (argc > 1) ? atol(argv[1]) : 400;
    size_t n_in = (size_t)W * H * 2u;
    size_t cap = pfc_bound(PFC_CODEC_IMAGE, n_in);
    uint8_t *src = malloc(n_in), *dec = malloc(n_in), *enc = malloc(cap), *ref = malloc(cap);
    pfc_ctx *w = malloc(pfc_workmem_bytes());
    pfc_params p;
    size_t ref_len = 0, i;
    long ref_calls;
    int r;
    /* [region][silent?][detected?] */
    long tab[REG_N][2][2];

    memset(tab, 0, sizeof tab);
    bounds_init();
    memset(&p, 0, sizeof p);
    p.width = W; p.height = H; p.bitdepth = 16u;

    g_rng = 12345u;
    for (i = 0u; i < (size_t)(W * H); i++) {
        uint32_t v = (uint32_t)((i % W) * 7u) + (uint32_t)((i / W) * 3u) + (rnd() & 0x1Fu);
        ((uint16_t *)src)[i] = (uint16_t)v;
    }

    g_inject_at = -1; g_call_count = 0; g_live = NULL;
    if (pfc_encode(PFC_CODEC_IMAGE, &p, src, n_in, ref, cap, &ref_len, w) != PFC_OK) {
        fprintf(stderr, "ref encode failed\n"); return 2;
    }
    ref_calls = g_call_count;

    printf("SEU detectability via the free model invariant  tot[ctx] == sum(freq[ctx][*])\n");
    printf("  image %ux%u @16-bit, %ld resid calls, %ld trials/region\n\n", W, H, ref_calls, per_region);

    g_rng = 4242u;
    for (r = 0; r < REG_N; r++) {
        long t;
        for (t = 0; t < per_region; t++) {
            size_t off = rs[r] + (size_t)((((uint64_t)rnd() << 8) | (rnd() & 0xFFu)) % (re[r] - rs[r]));
            size_t enc_len = 0, dec_len = 0;
            int silent = 0, detected;
            pfc_status st;

            g_inject_at = (long)(rnd() % (uint32_t)ref_calls);
            g_inject_byte = off; g_inject_bit = rnd() & 7u;
            g_call_count = 0; g_violation_seen = 0; g_live = NULL;

            st = pfc_encode(PFC_CODEC_IMAGE, &p, src, n_in, enc, cap, &enc_len, w);
            /* final block's state is never followed by a reset -- check it explicitly */
            if ((g_live != NULL) && (!invariant_holds(w))) { g_violation_seen = 1; }
            detected = g_violation_seen;

            if (st == PFC_OK) {
                if (!((enc_len == ref_len) && (memcmp(enc, ref, enc_len) == 0))) {
                    memset(dec, 0, n_in);
                    st = pfc_decode(enc, enc_len, dec, n_in, &dec_len, w);
                    if ((st == PFC_OK) && (memcmp(dec, src, n_in) != 0)) { silent = 1; }
                }
            }
            tab[r][silent][detected]++;
        }
    }

    printf("%-12s %10s %10s %12s %14s\n", "region", "silent", "caught", "missed", "detect rate");
    printf("%s\n", "---------------------------------------------------------------------");
    {
        long tot_sil = 0, tot_caught = 0;
        for (r = 0; r < REG_N; r++) {
            long sil = tab[r][1][0] + tab[r][1][1];
            long caught = tab[r][1][1];
            tot_sil += sil; tot_caught += caught;
            printf("%-12s %10ld %10ld %12ld %13s\n", rname[r], sil, caught, sil - caught,
                   (sil == 0) ? "n/a" : "");
            if (sil > 0) {
                printf("%-12s %10s %10s %12s %12.1f%%\n", "", "", "", "",
                       100.0 * (double)caught / (double)sil);
            }
        }
        printf("%s\n", "---------------------------------------------------------------------");
        printf("%-12s %10ld %10ld %12ld %12.1f%%\n", "ALL", tot_sil, tot_caught,
               tot_sil - tot_caught,
               (tot_sil == 0) ? 0.0 : 100.0 * (double)tot_caught / (double)tot_sil);

        /* False positives: the check firing when nothing bad happened. A noisy check that
         * cries wolf on harmless upsets would be a liability in flight, so measure it. */
        {
            long harmless = 0, fp = 0;
            for (r = 0; r < REG_N; r++) { harmless += tab[r][0][0] + tab[r][0][1]; fp += tab[r][0][1]; }
            printf("\nFalse-positive check: %ld of %ld NON-silent trials also tripped the invariant "
                   "(%.1f%%).\n", fp, harmless, (harmless == 0) ? 0.0 : 100.0 * (double)fp / (double)harmless);
            printf("(Those are real corruptions of the model that happened not to change the output --\n");
            printf(" flagging them is conservative and correct, not a false alarm in the usual sense.)\n");
        }
    }

    free(src); free(dec); free(enc); free(ref); free(w);
    return 0;
}
