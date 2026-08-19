/* fuzz_pfc.c — libFuzzer harness for the decoder (untrusted downlink input).
 * SPDX-License-Identifier: Apache-2.0
 *
 * Build (needs clang):
 *   clang -std=c99 -g -O1 -fsanitize=address,undefined,fuzzer -Iinclude \
 *         src/*.c test/fuzz_pfc.c -o build/fuzz_pfc
 *   ./build/fuzz_pfc -max_len=65536
 *
 * The decoder must never read out of bounds or crash on ANY input (R6); ASan+UBSan+fuzzer
 * enforce that. Not run in this build environment (no clang); wired here for CI.
 */
#include "pfc.h"
#include <stdint.h>
#include <stddef.h>
#include <stdlib.h>

static struct pfc_ctx *g_work; /* lazily allocated once */
static unsigned char g_dst[1u << 22]; /* 4 MB output cap */

int LLVMFuzzerTestOneInput(const uint8_t *data, size_t size)
{
    size_t out = 0;
    if (g_work == NULL) {
        g_work = (struct pfc_ctx *)malloc(pfc_workmem_bytes());
        if (g_work == NULL) {
            return 0;
        }
    }
    (void)pfc_decode(data, size, g_dst, sizeof g_dst, &out, g_work);
    return 0;
}
