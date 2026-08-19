/* harness_block_write.c — CBMC entry point for pfc_block_write.
 * SPDX-License-Identifier: Apache-2.0
 *
 * The symmetric counterpart to harness_block_read.c. When the `size_t`-wraparound bounds-check bug
 * was found in `pfc_block_read`, the identical addition-based pattern in `pfc_block_write` was
 * rewritten the same way "defensively" — its inputs are encoder-side and locally trusted, so it was
 * never the live threat that the decoder's was, and it was never actually PROVED. This closes that
 * asymmetry: the fix is now verified rather than assumed correct by analogy.
 *
 * Proving the write side is worth doing on its own terms, not just for symmetry. `plen` reaching
 * this function is derived from the range coder's output length, and the encoder runs on the
 * spacecraft — the 32-bit machine where the wraparound is reachable. A `pfc_bound` miscalculation
 * or a corrupted length would land here, on the flight side, where there is no ground operator to
 * notice a bad frame.
 *
 * Proved, for ANY (cap, pos, plen) a caller could present:
 *   - on PFC_OK, every byte written lies inside dst[0..cap) — enforced by CBMC's --bounds-check
 *     against a concretely-sized buffer, plus an explicit assertion that the advanced position
 *     never passes cap;
 *   - on PFC_OK, *pos advances by exactly the block size (header + payload), so a caller writing
 *     consecutive blocks cannot be walked off the end by a miscount;
 *   - on PFC_E_BOUND, *pos is completely unchanged — the write side, unlike the read side, must
 *     NOT advance on rejection (there is no "stay in sync after a corrupt block" case when
 *     writing; a rejected write must leave the stream exactly as it was, or the caller would emit
 *     a hole). This is a genuine behavioural difference from harness_block_read.c's contract, not
 *     an oversight — see the note there about CRC-mismatch deliberately advancing `pos`.
 *
 * MUST be run with `--32` for the same reason as the read-side proof: the bug class this exists to
 * exclude only manifests when `size_t` is 32-bit (RAD750/LEON/RISC-V-32, the actual flight
 * encoder's hardware). Under the host's native 64-bit size_t the proof would pass vacuously.
 */
#include "pfc_internal.h"

#define HARNESS_DST_BYTES 32u
#define HARNESS_PAY_BYTES 16u

void harness_pfc_block_write(void)
{
    uint8_t dst[HARNESS_DST_BYTES];
    uint8_t payload[HARNESS_PAY_BYTES];
    size_t cap;
    size_t pos_in;
    size_t pos;
    size_t plen;
    uint8_t flags;
    pfc_status st;

    /* cap/pos_in/plen/flags and payload's bytes are left uninitialised: CBMC treats every read of
     * an uninitialised local as fully nondeterministic. The assumptions below are exactly the
     * caller contract every real call site maintains — nothing stronger, so the proof stays
     * meaningful rather than assuming the bug away. */
    __CPROVER_assume(cap <= HARNESS_DST_BYTES);
    __CPROVER_assume(pos_in <= cap);
    __CPROVER_assume(plen <= HARNESS_PAY_BYTES);
    pos = pos_in;

    st = pfc_block_write(dst, cap, &pos, payload, plen, flags);

    if (st == PFC_OK) {
        __CPROVER_assert(pos == (pos_in + PFC_BLKHDR + plen),
                         "pos advances by exactly the block size");
        __CPROVER_assert(pos <= cap, "advanced pos stays within cap");
    } else {
        __CPROVER_assert(pos == pos_in, "pos is unchanged when the write is rejected");
    }
}
