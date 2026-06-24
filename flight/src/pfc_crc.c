/* pfc_crc.c — CRC-32 (IEEE 802.3, reflected), table-free for a small, fixed footprint.
 * SPDX-License-Identifier: Apache-2.0
 *
 * Used to detect a corrupted downlink block so its damage is contained (R6).
 */
#include "pfc_internal.h"

uint32_t pfc_crc32(const uint8_t *buf, size_t len)
{
    uint32_t crc = 0xFFFFFFFFu;
    size_t i;
    unsigned b;
    for (i = 0u; i < len; i++) {
        crc ^= buf[i];
        for (b = 0u; b < 8u; b++) {
            uint32_t mask = 0u - (crc & 1u);
            crc = (crc >> 1) ^ (0xEDB88320u & mask);
        }
    }
    return ~crc;
}
