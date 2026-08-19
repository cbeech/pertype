/* harness_block_read.c — CBMC entry point for pfc_block_read.
 * SPDX-License-Identifier: Apache-2.0
 *
 * Proves the documented contract of pfc_block_read (see pfc_frame.c) holds for ANY bytes an
 * attacker or downlink corruption could put in src, regardless of size_t width:
 *   - on PFC_OK, payload/plen lie entirely within src[0..len) and pos advances by exactly the
 *     block size;
 *   - on PFC_E_CORRUPT, pos never moves backward and never advances past len -- "reads never
 *     advance out of bounds" (see pfc_frame.c's header comment), not "pos is always unchanged":
 *     a CRC-mismatch rejection deliberately DOES advance pos (see the comment at that return in
 *     pfc_block_read) so a caller can keep reading subsequent blocks after a corrupt one -- an
 *     earlier version of this harness wrongly asserted pos was unchanged on every rejection and
 *     CBMC correctly flagged that as false.
 *
 * MUST be run with `--32` (see Makefile `cbmc` target / flight-ci.yml `cbmc` job): the property
 * this proof exists for is a size_t-width-dependent bug — a crafted payload_len field near
 * UINT32_MAX can wrap the bounds check on a 32-bit size_t target (RAD750/LEON/RISC-V-32, the
 * actual flight targets) while never doing so on the 64-bit dev/CI host that every other gate
 * (native/libfuzzer/bigendian) runs on. Running this harness under the host's native 64-bit
 * size_t would silently prove nothing about the bug class it targets.
 */
#include "pfc_internal.h"

#define HARNESS_BUF_BYTES 32u

void harness_pfc_block_read(void)
{
    uint8_t src[HARNESS_BUF_BYTES];
    size_t len;
    size_t pos_in;
    size_t pos;
    const uint8_t *payload;
    size_t plen;
    uint8_t flags;
    pfc_status st;

    /* src's bytes and (len, pos_in) are left uninitialised: CBMC treats every read of an
     * uninitialised local as fully nondeterministic -- exactly "any bytes on the wire" for src,
     * and "any (len, pos) a caller could legally be at" for the rest. */
    __CPROVER_assume(len <= HARNESS_BUF_BYTES);
    __CPROVER_assume(pos_in <= len);   /* the invariant every call site maintains */
    pos = pos_in;

    st = pfc_block_read(src, len, &pos, &payload, &plen, &flags);

    if (st == PFC_OK) {
        __CPROVER_assert(payload >= src, "payload starts within src");
        __CPROVER_assert((size_t)(payload - src) + plen <= len, "payload+plen within src bounds");
        __CPROVER_assert(pos == (pos_in + PFC_BLKHDR + plen), "pos advances by exactly the block size");
        __CPROVER_assert(pos <= len, "advanced pos stays within len");
    } else {
        __CPROVER_assert(pos >= pos_in, "pos never moves backward on rejection");
        __CPROVER_assert(pos <= len, "pos never advances past len on rejection");
    }
}
