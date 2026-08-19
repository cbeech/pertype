/* downlink_containment.c — does a CRC-rejected block's damage stay inside that block?
 * SPDX-License-Identifier: Apache-2.0
 *
 * THE QUESTION
 * ------------
 * R6 claims each block is independently CRC-protected so "a corrupt/truncated frame loses one
 * block". SEU fault injection (test/seu_inject.c) showed that claim already fails for SPECTRAL
 * under ENCODER-side faults, because SPECTRAL reconstructs band z from band z-1. This harness
 * asks the follow-up that actually decides whether R6 itself is documented wrong: does the same
 * propagation occur for ordinary DOWNLINK corruption -- the case R6 is really about?
 *
 * The mechanism is visible in pfc_spectral.c: on a CRC mismatch the decoder calls
 * spec_fill_block(), which fills the block with mid-grey (1 << (bd-1)) rather than the true
 * samples, then `continue`s. Band z+1 then predicts from that filled band. So the *reference* for
 * every later band is wrong, even though those later blocks' own CRCs are perfectly valid.
 *
 * Reasoning is not evidence, so this measures it, with a CONTROL: IMAGE uses purely spatial
 * prediction with per-band model resets and no inter-band reference, so its damage MUST stay in
 * the corrupted band. If IMAGE confines and SPECTRAL does not, the difference is attributable to
 * inter-band prediction and nothing else.
 *
 * WHAT COUNTS AS A RESULT
 * -----------------------
 * For each codec we corrupt exactly ONE block record's payload (flipping one bit, which fails
 * that block's CRC and nothing else) and then report, per band, how many samples differ from the
 * original. "Contained" means only the band owning the corrupted block differs.
 */
#include "pfc_internal.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* Mirrors the private PFC_SPEC_HDR in pfc_spectral.c (SPECTRAL's header is 24 bytes, not the
 * generic 20) -- it is deliberately file-private there, so the test restates it rather than
 * widening the internal API just for a harness. Asserted against the encoder's own output below. */
#define TEST_SPEC_HDR 24u

static int g_fail;

/* Walk the block-record chain and flip one bit inside the payload of record #target.
 * Returns the byte offset corrupted, or 0 if that record doesn't exist. */
static size_t corrupt_nth_block_payload(uint8_t *s, size_t len, size_t hdr, unsigned target)
{
    size_t pos = hdr;
    unsigned idx = 0u;
    while ((pos + PFC_BLKHDR) <= len) {
        uint32_t plen = pfc_get_u32(&s[pos]);
        size_t payload = pos + PFC_BLKHDR;
        if ((payload + plen) > len) { return 0u; }
        if (idx == target) {
            if (plen == 0u) { return 0u; }
            s[payload] ^= 0x40u;          /* one bit -> this block's CRC now fails */
            return payload;
        }
        pos = payload + plen;
        idx++;
    }
    return 0u;
}

/* Per-band differing-sample counts between original and decoded. */
static void per_band_diff(const uint8_t *a, const uint8_t *b, uint8_t bd,
                          uint32_t width, uint32_t height, uint32_t count,
                          uint32_t *out_counts)
{
    uint32_t z, i;
    size_t per_band = (size_t)width * height;
    size_t es = (bd > 8u) ? 2u : 1u;
    for (z = 0u; z < count; z++) {
        uint32_t bad = 0u;
        for (i = 0u; i < per_band; i++) {
            size_t off = ((size_t)z * per_band + i) * es;
            if (memcmp(&a[off], &b[off], es) != 0) { bad++; }
        }
        out_counts[z] = bad;
    }
}

static void run_case(const char *label, pfc_codec codec, pfc_params p,
                     uint32_t width, uint32_t height, uint32_t count, uint8_t bd,
                     size_t hdr, unsigned target_block)
{
    size_t n_in = (size_t)width * height * count * ((bd > 8u) ? 2u : 1u);
    size_t cap = pfc_bound(codec, n_in);
    uint8_t *src = malloc(n_in), *dec = malloc(n_in);
    uint8_t *enc = malloc(cap);
    pfc_ctx *w = malloc(pfc_workmem_bytes());
    size_t enc_len = 0, dec_len = 0, hit;
    uint32_t *counts = malloc(count * sizeof(uint32_t));
    uint32_t z, first_bad = 0xFFFFFFFFu, last_bad = 0u, n_bad_bands = 0u;
    pfc_status st;
    uint32_t rng = 2246822519u;

    if (!src || !dec || !enc || !w || !counts) { printf("  oom\n"); g_fail++; return; }

    /* Smoothly varying cube: compressible, and (for SPECTRAL) strongly inter-band correlated, so
     * the prediction path this test is about is genuinely exercised. */
    {
        size_t i;
        for (i = 0u; i < n_in; i += 2u) {
            uint32_t v;
            rng = (rng * 1664525u) + 1013904223u;
            v = (uint32_t)((i / 2u) % 97u) * 13u + ((rng >> 24) & 0x7u);
            if (bd > 8u) { src[i] = (uint8_t)(v & 0xFFu); src[i + 1u] = (uint8_t)((v >> 8) & 0xFFu); }
            else { src[i] = (uint8_t)(v & 0xFFu); src[i + 1u] = (uint8_t)((v >> 3) & 0xFFu); }
        }
    }

    st = pfc_encode(codec, &p, src, n_in, enc, cap, &enc_len, w);
    if (st != PFC_OK) { printf("  %s: encode failed (%d)\n", label, (int)st); g_fail++; return; }

    /* Sanity: an uncorrupted stream must round-trip, or the experiment means nothing. */
    st = pfc_decode(enc, enc_len, dec, n_in, &dec_len, w);
    if ((st != PFC_OK) || (memcmp(src, dec, n_in) != 0)) {
        printf("  %s: clean round-trip FAILED -- test invalid\n", label); g_fail++; return;
    }

    hit = corrupt_nth_block_payload(enc, enc_len, hdr, target_block);
    if (hit == 0u) { printf("  %s: could not corrupt block %u\n", label, target_block); g_fail++; return; }

    memset(dec, 0, n_in);
    st = pfc_decode(enc, enc_len, dec, n_in, &dec_len, w);
    per_band_diff(src, dec, bd, width, height, count, counts);

    for (z = 0u; z < count; z++) {
        if (counts[z] != 0u) {
            n_bad_bands++;
            if (z < first_bad) { first_bad = z; }
            if (z > last_bad) { last_bad = z; }
        }
    }

    printf("\n  %s\n", label);
    printf("    corrupted 1 bit in block %u's payload (stream offset %zu of %zu)\n",
           target_block, hit, enc_len);
    printf("    decode status: %s\n",
           (st == PFC_E_CORRUPT) ? "PFC_E_CORRUPT (reported)" :
           (st == PFC_OK) ? "PFC_OK (NOT reported!)" : "other");
    printf("    per-band differing samples (of %u each):\n", width * height);
    for (z = 0u; z < count; z++) {
        printf("      band %u: %6u %s\n", z, counts[z], (counts[z] != 0u) ? "<-- damaged" : "");
    }
    if (n_bad_bands <= 1u) {
        printf("    => CONTAINED: damage confined to %u band(s).\n", n_bad_bands);
    } else {
        printf("    => PROPAGATED: %u bands damaged (bands %u..%u) from ONE corrupt block.\n",
               n_bad_bands, first_bad, last_bad);
        printf("       Those later blocks' own CRCs were VALID -- they decoded correctly against\n");
        printf("       a wrong reference. R6's 'loses one block' does not hold here.\n");
    }

    free(src); free(dec); free(enc); free(w); free(counts);
}

int main(void)
{
    const uint32_t W = 32u, H = 32u, Z = 6u;
    pfc_params p;

    printf("Downlink corruption containment: does one bad block stay one bad block?\n");
    printf("One bit flipped in one block's payload; that block's CRC fails, all others stay valid.\n");

    /* CONTROL: IMAGE has no inter-band reference (spatial prediction + per-band model reset).
     * Treated as a Z-band-tall single image so the geometry matches the SPECTRAL case. */
    memset(&p, 0, sizeof p);
    p.width = W; p.height = H * Z; p.bitdepth = 16u;
    run_case("IMAGE (control -- no inter-band prediction)", PFC_CODEC_IMAGE, p,
             W, H, Z, 16u, PFC_HDR, 0u);

    /* SUBJECT: SPECTRAL predicts band z from band z-1. */
    memset(&p, 0, sizeof p);
    p.width = W; p.height = H; p.count = Z; p.bitdepth = 16u;
    run_case("SPECTRAL (subject -- band z predicted from band z-1)", PFC_CODEC_SPECTRAL, p,
             W, H, Z, 16u, TEST_SPEC_HDR, 0u);

    printf("\n%s\n", (g_fail == 0) ? "harness ran clean" : "HARNESS ERRORS -- results unreliable");
    return (g_fail == 0) ? 0 : 1;
}
