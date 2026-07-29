/* seu_inject.c — single-event-upset fault injection into the ENCODER's working memory.
 * SPDX-License-Identifier: Apache-2.0
 *
 * WHY THIS EXISTS
 * ---------------
 * docs/mission-safety.md §2.5 makes a claim that, until now, was reasoned rather than measured:
 * the CRC protects the *downlink channel*, but a single-event upset that flips a bit in the
 * encoder's working memory (`pfc_ctx`: context tables, range-coder state, scratch buffers)
 * mid-encode is a different fault entirely — the CRC is computed *after* the corruption, over the
 * already-wrong payload, so a corrupted block can be perfectly self-consistent and pass CRC on the
 * ground. The mitigating argument was that small independent blocks bound the blast radius.
 *
 * This harness measures both halves of that claim instead of asserting them:
 *   1. How often does an encoder-side SEU produce SILENTLY WRONG output (decode returns PFC_OK but
 *      the data is not what was encoded)? That is the genuinely dangerous outcome — no CRC, no
 *      status code, no indication anything happened.
 *   2. When it does, is the damage actually confined to one block, or does it spread?
 *
 * HOW IT INJECTS WITHOUT TOUCHING FLIGHT SOURCE
 * ---------------------------------------------
 * Fault injection normally means putting `#ifdef INJECT` hooks in the code under test, which would
 * mean shipping test-only branches inside MISRA-reviewed flight source — unacceptable. Instead
 * this uses the GNU linker's `--wrap` feature: linking with `-Wl,--wrap=pfc_resid_encode` makes
 * every call to `pfc_resid_encode` resolve to `__wrap_pfc_resid_encode` (below), which can flip a
 * bit and then forward to the untouched original via `__real_pfc_resid_encode`. `src/` is compiled
 * completely unmodified — the same object code the flight build produces.
 *
 * `pfc_resid_encode` is the right interception point because every codec calls it once per sample,
 * giving single-sample granularity for "the upset happened *here* during the encode", rather than
 * only at block boundaries.
 *
 * TWO SAMPLING MODES, AND WHY BOTH ARE NEEDED
 * -------------------------------------------
 * `pfc_ctx` is ~330 KB, but ~99% of it is `scratch[]` + `xform[]` — buffers that are overwritten
 * before use, so a flip there is usually harmless. Sampling bit positions uniformly therefore
 * measures the *natural* rate (what a real orbit would see) but barely samples the small,
 * high-value model tables: a run that hits `tot[]` twice tells you nothing useful about `tot[]`.
 * So this runs two passes:
 *   UNIFORM    — bit position uniform over all of pfc_ctx. Answers "what happens in orbit?"
 *   STRATIFIED — equal trials per region. Answers "how dangerous is *this* region, per bit?",
 *                which is what tells you which regions are worth protecting with EDAC/scrubbing.
 * The stratified numbers are conditional risks, NOT orbit rates — weight them by each region's
 * size before drawing any mission-level conclusion. Both are reported separately for that reason.
 *
 * WHAT A RESULT MEANS
 * -------------------
 *   CLEAN    — output byte-identical to an uncorrupted encode, or still decodes to the right data.
 *   DETECTED — decode reported PFC_E_CORRUPT. Safe-ish: the ground station knows.
 *   SILENT   — decode reported PFC_OK but the data differs. THE DANGEROUS CASE.
 *   OTHER    — some other non-OK status; reported separately, not conflated with either.
 *
 * Note the asymmetry this harness exists to expose: DETECTED is a *success* of the design, SILENT
 * is the failure mode no other gate in this project can see. libFuzzer, ASan, the CRC tests and
 * the CBMC proof all target the *decoder* eating bad input; none of them models the encoder's own
 * memory being wrong while it runs.
 */
#include "pfc_internal.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Provided by the linker's --wrap: the untouched original. */
void __real_pfc_resid_encode(pfc_rc_enc *e, pfc_ctx *w, unsigned ctx, int32_t resid);

/* ---- injection state ------------------------------------------------------------------- */
static long     g_call_count;      /* pfc_resid_encode calls so far this encode */
static long     g_inject_at;       /* inject when g_call_count hits this (-1 = never) */
static size_t   g_inject_byte;     /* byte offset within pfc_ctx to corrupt */
static unsigned g_inject_bit;      /* which bit of that byte */

void __wrap_pfc_resid_encode(pfc_rc_enc *e, pfc_ctx *w, unsigned ctx, int32_t resid)
{
    if ((g_inject_at >= 0) && (g_call_count == g_inject_at)) {
        ((uint8_t *)w)[g_inject_byte] ^= (uint8_t)(1u << g_inject_bit);
    }
    g_call_count++;
    __real_pfc_resid_encode(e, w, ctx, resid);
}

/* ---- deterministic RNG (reproducible runs; no time(), no rand()) ------------------------ */
static uint32_t g_rng;
static uint32_t rnd(void)
{
    g_rng = (g_rng * 1664525u) + 1013904223u;
    return g_rng >> 8;
}

/* ---- pfc_ctx regions -------------------------------------------------------------------
 * Classifying the hit region is the difference between "SEUs are mostly harmless" (true but
 * uninteresting, since most of pfc_ctx is scratch) and a per-region risk picture, which is what a
 * fault model actually needs. Bounds are derived from the real struct, not hardcoded. */
typedef enum { REG_FREQ, REG_TOT, REG_MANT, REG_BIAS, REG_SCRATCH, REG_XFORM, REG_N } region;
static const char *region_name[REG_N] = {
    "freq[][] (category model)", "tot[] (context totals)", "mant[][] (mantissa model)",
    "bias_*[] (image bias)", "scratch[] (block payload)", "xform[] (de-interleave)"
};
static size_t reg_start[REG_N], reg_end[REG_N];

static void region_bounds_init(void)
{
    struct pfc_ctx probe;
    size_t base = (size_t)(uintptr_t)&probe;
    reg_start[REG_FREQ]    = (size_t)(uintptr_t)&probe.freq    - base;
    reg_start[REG_TOT]     = (size_t)(uintptr_t)&probe.tot     - base;
    reg_start[REG_MANT]    = (size_t)(uintptr_t)&probe.mant    - base;
    reg_start[REG_BIAS]    = (size_t)(uintptr_t)&probe.bias_c  - base;
    reg_start[REG_SCRATCH] = (size_t)(uintptr_t)&probe.scratch - base;
    reg_start[REG_XFORM]   = (size_t)(uintptr_t)&probe.xform   - base;
    reg_end[REG_FREQ]    = reg_start[REG_TOT];
    reg_end[REG_TOT]     = reg_start[REG_MANT];
    reg_end[REG_MANT]    = reg_start[REG_BIAS];
    reg_end[REG_BIAS]    = reg_start[REG_SCRATCH];
    reg_end[REG_SCRATCH] = reg_start[REG_XFORM];
    reg_end[REG_XFORM]   = sizeof(struct pfc_ctx);
}

static region classify(size_t off)
{
    int r;
    for (r = 0; r < REG_N; r++) {
        if ((off >= reg_start[r]) && (off < reg_end[r])) { return (region)r; }
    }
    return REG_XFORM;
}

/* ---- outcome bookkeeping ---------------------------------------------------------------- */
typedef enum { OUT_CLEAN, OUT_DETECTED, OUT_SILENT, OUT_OTHER, OUT_N } outcome;
static const char *outcome_name[OUT_N] = {
    "CLEAN (no observable effect)", "DETECTED (decode reported corruption)",
    "SILENT (decode said OK, data wrong)", "OTHER (non-OK, non-corrupt status)"
};

typedef struct {
    long tally[OUT_N];
    long region_tally[REG_N][OUT_N];
    long silent_samples_total;
    long silent_samples_worst;
    long silent_multiblock;     /* silent corruptions spanning >1 band */
    long trials;
} results;

/* Shared fixture for a run. */
typedef struct {
    pfc_params p;
    uint32_t   w, h;
    size_t     n_in;
    uint8_t   *src, *dec, *ref_enc, *enc;
    pfc_ctx   *work;
    size_t     cap, ref_len;
    long       ref_calls;
} fixture;

static outcome one_trial(fixture *f, size_t off, unsigned bit, long at, results *res)
{
    size_t enc_len = 0, dec_len = 0;
    pfc_status st;

    g_inject_at = at; g_inject_byte = off; g_inject_bit = bit; g_call_count = 0;

    st = pfc_encode(PFC_CODEC_IMAGE, &f->p, f->src, f->n_in, f->enc, f->cap, &enc_len, f->work);
    if (st != PFC_OK) { return OUT_OTHER; }
    if ((enc_len == f->ref_len) && (memcmp(f->enc, f->ref_enc, enc_len) == 0)) {
        return OUT_CLEAN;   /* corruption never reached the output at all */
    }

    memset(f->dec, 0, f->n_in);
    st = pfc_decode(f->enc, enc_len, f->dec, f->n_in, &dec_len, f->work);
    if (st == PFC_E_CORRUPT) { return OUT_DETECTED; }
    if (st != PFC_OK)        { return OUT_OTHER; }
    if (memcmp(f->dec, f->src, f->n_in) == 0) { return OUT_CLEAN; }

    /* SILENT: measure the blast radius, which is the containment claim under test. */
    {
        const uint16_t *a = (const uint16_t *)f->src, *b = (const uint16_t *)f->dec;
        uint32_t i, bad = 0u, first_band = 0xFFFFFFFFu, last_band = 0u;
        for (i = 0u; i < (f->w * f->h); i++) {
            if (a[i] != b[i]) {
                uint32_t band = (i / f->w) / PFC_BAND_ROWS;
                bad++;
                if (band < first_band) { first_band = band; }
                if (band > last_band)  { last_band = band; }
            }
        }
        res->silent_samples_total += bad;
        if ((long)bad > res->silent_samples_worst) { res->silent_samples_worst = (long)bad; }
        if (last_band != first_band) { res->silent_multiblock++; }
    }
    return OUT_SILENT;
}

/* target < 0 => uniform over all of pfc_ctx; otherwise confine offsets to that region. */
static void run_pass(fixture *f, int target, long trials, results *res)
{
    long t;
    memset(res, 0, sizeof *res);
    res->trials = trials;
    for (t = 0; t < trials; t++) {
        size_t lo = (target < 0) ? 0u : reg_start[target];
        size_t hi = (target < 0) ? sizeof(struct pfc_ctx) : reg_end[target];
        size_t span = hi - lo;
        size_t off = lo + (size_t)((((uint64_t)rnd() << 8) | (rnd() & 0xFFu)) % span);
        outcome r = one_trial(f, off, rnd() & 7u, (long)(rnd() % (uint32_t)f->ref_calls), res);
        res->tally[r]++;
        res->region_tally[classify(off)][r]++;
    }
}

static void print_outcomes(const results *res)
{
    int i;
    for (i = 0; i < OUT_N; i++) {
        printf("  %-38s %6ld  (%5.1f%%)\n", outcome_name[i], res->tally[i],
               (100.0 * (double)res->tally[i]) / (double)res->trials);
    }
}

int main(int argc, char **argv)
{
    const uint32_t W = 64u, H = 64u;      /* 4 bands at PFC_BAND_ROWS=16 -> containment is testable */
    long trials = (argc > 1) ? atol(argv[1]) : 3000;
    long per_region = (trials / 4) / REG_N;
    fixture f;
    results uni, strat[REG_N];
    pfc_status st;
    int r;

    region_bounds_init();

    memset(&f, 0, sizeof f);
    f.w = W; f.h = H;
    f.n_in = (size_t)W * H * 2u;
    f.p.width = W; f.p.height = H; f.p.bitdepth = 16u;
    f.cap = pfc_bound(PFC_CODEC_IMAGE, f.n_in);
    f.src = malloc(f.n_in); f.dec = malloc(f.n_in);
    f.ref_enc = malloc(f.cap); f.enc = malloc(f.cap);
    f.work = malloc(pfc_workmem_bytes());
    if (!f.src || !f.dec || !f.ref_enc || !f.enc || !f.work) { fprintf(stderr, "oom\n"); return 2; }

    /* Gently-textured image: compressible enough to exercise the model (not store-raw), noisy
     * enough that residuals vary and the adaptive tables actually get used. */
    g_rng = 12345u;
    {
        uint16_t *s16 = (uint16_t *)f.src;
        uint32_t i;
        for (i = 0u; i < (W * H); i++) {
            s16[i] = (uint16_t)(((i % W) * 7u) + ((i / W) * 3u) + (rnd() & 0x1Fu));
        }
    }

    g_inject_at = -1; g_call_count = 0;
    st = pfc_encode(PFC_CODEC_IMAGE, &f.p, f.src, f.n_in, f.ref_enc, f.cap, &f.ref_len, f.work);
    if (st != PFC_OK) { fprintf(stderr, "reference encode failed: %d\n", (int)st); return 2; }
    f.ref_calls = g_call_count;

    printf("SEU injection into encoder working memory (pfc_ctx = %zu B)\n", pfc_workmem_bytes());
    printf("  image %ux%u @16-bit, %ld resid-encode calls\n", W, H, f.ref_calls);
    printf("  region sizes:\n");
    for (r = 0; r < REG_N; r++) {
        printf("    %-28s %8zu B (%5.2f%%)\n", region_name[r], reg_end[r] - reg_start[r],
               (100.0 * (double)(reg_end[r] - reg_start[r])) / (double)sizeof(struct pfc_ctx));
    }

    /* ---- pass 1: uniform (the orbit rate) ---- */
    g_rng = 99991u;
    printf("\n===== PASS 1: UNIFORM over all of pfc_ctx (%ld trials) =====\n", trials);
    printf("Approximates the real in-orbit rate: an upset is equally likely at any bit.\n\n");
    run_pass(&f, -1, trials, &uni);
    print_outcomes(&uni);

    /* ---- pass 2: stratified (per-region conditional risk) ---- */
    printf("\n===== PASS 2: STRATIFIED, %ld trials per region =====\n", per_region);
    printf("Per-region CONDITIONAL risk -- 'given an upset lands here, what happens?'\n");
    printf("These are NOT orbit rates; weight by region size (above) before concluding.\n\n");
    printf("  %-28s %7s %7s %7s %7s\n", "region", "clean", "detect", "SILENT", "other");
    for (r = 0; r < REG_N; r++) {
        run_pass(&f, r, per_region, &strat[r]);
        printf("  %-28s %7ld %7ld %7ld %7ld\n", region_name[r],
               strat[r].tally[OUT_CLEAN], strat[r].tally[OUT_DETECTED],
               strat[r].tally[OUT_SILENT], strat[r].tally[OUT_OTHER]);
    }

    printf("\n  silent-corruption rate by region (conditional):\n");
    for (r = 0; r < REG_N; r++) {
        printf("    %-28s %6.2f%%\n", region_name[r],
               (100.0 * (double)strat[r].tally[OUT_SILENT]) / (double)per_region);
    }

    /* ---- containment: the §2.5 claim under test ---- */
    {
        long silent = uni.tally[OUT_SILENT], multi = uni.silent_multiblock;
        long tot_samples = uni.silent_samples_total, worst = uni.silent_samples_worst;
        for (r = 0; r < REG_N; r++) {
            silent += strat[r].tally[OUT_SILENT];
            multi  += strat[r].silent_multiblock;
            tot_samples += strat[r].silent_samples_total;
            if (strat[r].silent_samples_worst > worst) { worst = strat[r].silent_samples_worst; }
        }
        printf("\n===== CONTAINMENT (§2.5's 'small blocks bound the blast radius') =====\n");
        if (silent == 0) {
            printf("  no silent corruptions observed; containment untested this run.\n");
        } else {
            printf("  silent corruptions      : %ld (across both passes)\n", silent);
            printf("  mean corrupted samples  : %.1f of %u (one band = %u)\n",
                   (double)tot_samples / (double)silent, W * H, W * PFC_BAND_ROWS);
            printf("  worst corrupted samples : %ld of %u\n", worst, W * H);
            printf("  spanning >1 band        : %ld of %ld\n", multi, silent);
            if (multi == 0) {
                printf("  => CONTAINMENT HOLDS: no silent corruption crossed a band boundary.\n");
                printf("     Note the ceiling this implies: one band is %u samples, so a single\n",
                       W * PFC_BAND_ROWS);
                printf("     bit flip can still silently corrupt an ENTIRE band. Contained is not\n");
                printf("     the same as small.\n");
            } else {
                printf("  => CONTAINMENT VIOLATED: damage crossed a band boundary. §2.5 must be\n");
                printf("     corrected -- block independence does NOT bound encoder-side upsets.\n");
            }
        }
    }

    free(f.src); free(f.dec); free(f.ref_enc); free(f.enc); free(f.work);
    return 0;
}
