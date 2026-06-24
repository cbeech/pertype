# libpfc — requirements & traceability

Flight-software requirements for the pertype-flight lossless core, each with a verification method
and the test(s) that exercise it. IDs are stable; tests live in `flight/test/`.

## Requirements

| ID | Requirement | Verification |
|----|-------------|--------------|
| **R1** | **Lossless.** For every valid input, `decode(encode(x)) == x` byte-for-byte. | Test (round-trip on synthetic + real data) |
| **R2** | **No dynamic allocation.** The library calls no `malloc`/`free` and uses no recursion. All working memory is a caller-supplied `pfc_ctx`. | Inspection (no libc alloc symbols) + test (caller owns `pfc_ctx`) |
| **R3** | **Bounded memory.** Working set ≤ compile-time maxima (`PFC_MAX_COLS`, `PFC_BAND_ROWS`); footprint reported by `pfc_workmem_bytes()`. | Inspection + test (fixed `pfc_ctx` size) |
| **R4** | **Deterministic & portable.** Integer-only (no floating point); the wire format is canonical little-endian so a big-endian encoder and little-endian decoder interoperate. | Inspection (no FP types in `src/`) + analysis |
| **R5** | **No expansion.** Output never exceeds `pfc_bound()`; incompressible bands fall back to store-raw. | Test (random data within bound + store-raw path) |
| **R6** | **Error containment.** Each block is independently decodable with a CRC-32; a corrupted/truncated frame loses one block, is reported, and never reads out of bounds or crashes. | Test (bit-flip + truncation) + ASan/UBSan |
| **R7** | **Bit-exact reference.** The C core round-trips byte-identically with the Python/Rust pertype reference. | Bridge (`test/bench_real.py`) — losslessness verified; cross-codec parity is phase 3 |

## Traceability matrix

| Requirement | Test / evidence | Status |
|-------------|-----------------|--------|
| R1 Lossless | `test_pfc.c::roundtrip` (gradient8/16, odd-size, width-1); `bench_real.py` (real CyCIF 16-bit, 4/4 byte-exact) | ✅ verified |
| R2 No malloc | `src/` uses only `stddef/stdint`; no `malloc`/recursion. `test_pfc.c` allocates `pfc_ctx` on the host; the library never does | ✅ verified (host); flight: link without libc-alloc |
| R3 Bounded memory | `pfc_workmem_bytes()` = `sizeof(struct pfc_ctx)` = 262 960 B at defaults; tune via `-DPFC_MAX_COLS`/`-DPFC_BAND_ROWS` | ✅ verified |
| R4 Deterministic/portable | No `float`/`double` in `src/` (grep-clean); `pfc_put_u32`/`pfc_get_u32` canonical LE; range coder integer-only | ✅ verified (single-endian host); cross-endian: phase 3 |
| R5 No expansion | `test_pfc.c::roundtrip` random16/random8 stay ≤ `pfc_bound`; store-raw path exercised | ✅ verified |
| R6 Error containment | `test_pfc.c::test_fault_injection` (CRC catch + undamaged bands intact), `::test_truncation`; `make asan` clean | ✅ verified |
| R7 Bit-exact reference | `bench_real.py` round-trips real imagery losslessly through the C core | ⏳ partial — byte-identical cross-check vs Python/Rust encoder is phase 3 |

## Verification environment

- `make strict` — `-std=c99 -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wstrict-prototypes -Werror`: **47/47 pass, zero warnings**.
- `make asan` — AddressSanitizer + UndefinedBehaviorSanitizer: **47/47 pass, no diagnostics** (decoder is memory-safe on corrupt/truncated input).
- `make misra` — `cppcheck --addon=misra` MISRA-C:2012 gate (run in CI where cppcheck is installed; not available in this build environment).
- `bench_real.py` — real CyCIF 16-bit: **1.72× lossless, within −3.3% of JPEG-LS** (the CCSDS-123-class bar).

## Open items (phase 2 / 3)

- Phase 2: `pfc_seq` (1-D), `pfc_float` (byte-plane), `pfc_columnar` front-ends on the same backend; richer JPEG-LS-style gradient context to close the −3.3% and reach the research-measured wins.
- Phase 3: byte-identical cross-check vs the Python/Rust reference (R7 full); MISRA report; libFuzzer on the decoder; cross-compile validation (big-endian PowerPC / SPARC-LEON / RISC-V); CCSDS-121/123 head-to-head.
