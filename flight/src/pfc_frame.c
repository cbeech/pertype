/* pfc_frame.c — shared block-record framing used by every codec.
 * SPDX-License-Identifier: Apache-2.0
 *
 * Block record: payload_len(4) | flags(1) | crc32(4) | payload[payload_len].
 * Each block is independently decodable; a CRC mismatch is reported so the codec can repair just
 * that block (R6). Reads never advance out of bounds.
 */
#include "pfc_internal.h"

pfc_status pfc_block_write(uint8_t *dst, size_t cap, size_t *pos,
                           const uint8_t *payload, size_t plen, uint8_t flags)
{
    size_t p = *pos;
    size_t rem;
    uint32_t crc;
    size_t i;

    /* Subtraction-based, not addition-based: p+PFC_BLKHDR+plen can wrap size_t on a 32-bit
     * target even though it never does on a 64-bit dev/CI host. Bound each quantity against
     * already-validated headroom instead of summing untrusted-scale values first. */
    if ((p > cap) || ((cap - p) < PFC_BLKHDR)) {
        return PFC_E_BOUND;
    }
    rem = (cap - p) - PFC_BLKHDR;
    if (plen > rem) {
        return PFC_E_BOUND;
    }
    crc = pfc_crc32(payload, plen);
    pfc_put_u32(&dst[p], (uint32_t)plen);
    dst[p + 4u] = flags;
    pfc_put_u32(&dst[p + 5u], crc);
    p += PFC_BLKHDR;
    for (i = 0u; i < plen; i++) {
        dst[p + i] = payload[i];
    }
    *pos = p + plen;
    return PFC_OK;
}

pfc_status pfc_block_read(const uint8_t *src, size_t len, size_t *pos,
                          const uint8_t **payload, size_t *plen, uint8_t *flags)
{
    size_t p = *pos;
    size_t rem;
    uint32_t n, crc;

    if ((p > len) || ((len - p) < PFC_BLKHDR)) {
        return PFC_E_CORRUPT;          /* truncated header — *pos unchanged */
    }
    n = pfc_get_u32(&src[p]);
    *flags = src[p + 4u];
    crc = pfc_get_u32(&src[p + 5u]);

    /* n is a raw 4-byte field straight from untrusted downlink data — attacker/corruption-
     * controlled up to UINT32_MAX. Comparing against rem (subtraction of already-validated
     * quantities) instead of summing p+PFC_BLKHDR+n avoids a size_t wraparound that would
     * otherwise let a crafted length near UINT32_MAX bypass this check on a 32-bit size_t
     * target (RAD750/LEON/RISC-V-32) — invisible on a 64-bit dev/CI host, where the same sum
     * never overflows. See docs/misra-deviations.md / proofs/cbmc for the CBMC proof. */
    rem = (len - p) - PFC_BLKHDR;
    if ((size_t)n > rem) {
        return PFC_E_CORRUPT;          /* implausible/truncated length — *pos unchanged, no OOB */
    }
    *payload = &src[p + PFC_BLKHDR];
    *plen = n;
    *pos = p + PFC_BLKHDR + n;          /* length was in-bounds: safe to advance */

    if (pfc_crc32(*payload, n) != crc) {
        return PFC_E_CORRUPT;          /* corrupt block, but framing stays in sync */
    }
    return PFC_OK;
}
