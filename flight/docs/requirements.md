# libpfc — requirements & traceability

Flight-software requirements for the pertype-flight lossless core, each with a verification method
and the test(s) that exercise it. IDs are stable; tests live in `flight/test/` and `flight/ground/`.

## Requirements

| ID | Requirement | Verification |
|----|-------------|--------------|
| **R1** | **Lossless.** For every valid input, `decode(encode(x)) == x` byte-for-byte. | Test (round-trip, all 4 codecs, synthetic + real) |
| **R2** | **No dynamic allocation.** No `malloc`/`free`/recursion. Working memory is a caller-supplied `pfc_ctx`. | Inspection (no alloc symbols in the `.so`) + test |
| **R3** | **Bounded memory.** Working set ≤ compile-time maxima; footprint = `pfc_workmem_bytes()`. | Inspection + test |
| **R4** | **Deterministic & portable.** Integer-only; canonical little-endian wire format (big-endian encoder ⇄ little-endian decoder). | Inspection (no FP in `src/`) + independent LE decoder |
| **R5** | **No expansion.** Output never exceeds `pfc_bound()`; incompressible blocks store raw. | Test (random data within bound, all codecs) |
| **R6** | **Error containment.** Each block independently CRC-protected; a corrupt/truncated frame loses one block, is reported, never reads OOB or crashes. | Test (bit-flip + truncation) + ASan/UBSan + fuzz |
| **R7** | **Independent reference.** An independent implementation decodes the C encoder's output byte-for-byte. | Cross-check (pure-Python ground decoder vs C encoder) |

## Codec coverage (broad scope)

| Codec | Front-end | Status |
|-------|-----------|--------|
| `PFC_CODEC_IMAGE` | 2-D MED/LOCO-I predictor + gradient context | ✅ |
| `PFC_CODEC_SEQ` | 1-D order-1 delta (int8/16/32, signed/unsigned) | ✅ |
| `PFC_CODEC_FLOAT` | float32/64 byte-plane split | ✅ |
| `PFC_CODEC_COLUMNAR` | record de-interleave + per-plane delta | ✅ |

All four share one integer range coder, one adaptive category model, and one block-framing layer.

## Traceability matrix

| Requirement | Test / evidence | Status |
|-------------|-----------------|--------|
| R1 Lossless | `test_pfc.c` (image8/16, seq, float, columnar, odd sizes); `bench_real.py` (real CyCIF 4/4 byte-exact) | ✅ verified |
| R2 No malloc | `src/` uses only `stddef/stdint`; `nm -uD build/libpfc.so` shows **no alloc imports** | ✅ verified |
| R3 Bounded memory | `pfc_workmem_bytes()` = 329 096 B at defaults; tune via `-DPFC_MAX_COLS`/`-DPFC_BAND_ROWS` | ✅ verified |
| R4 Deterministic/portable | no `float`/`double` types in `src/`; the **explicit-LE pure-Python decoder** decodes the C output (`test_crosscheck.py`), and store-raw serialises LE → stream is canonical | ✅ stream verified; BE-hardware run pending (no toolchain in env) |
| R5 No expansion | `test_pfc.c` random inputs (all codecs) stay ≤ `pfc_bound`; store-raw path exercised | ✅ verified |
| R6 Error containment | `test_pfc.c::test_fault_injection`/`::test_truncation`; `make asan` clean; **`fuzz_decode.py` 20 000 random/mutated inputs, no crash/OOB** | ✅ verified |
| R7 Independent reference | `test_crosscheck.py`: C-encode → Python-decode == original, **7/7** incl. real CyCIF, all 4 codecs | ✅ verified |

## Verification environment (this build)

- `make strict` — `-std=c99 -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wstrict-prototypes -Werror`: **70/70, zero warnings**.
- `make asan` — ASan + UBSan: **70/70, no diagnostics**.
- `test_crosscheck.py` — C encoder vs independent Python decoder: **7/7 byte-exact** (incl. real CyCIF).
- `fuzz_decode.py` — **20 000 iterations**, decoder survived all random/mutated input.
- `ccsds_compare.py` — real CyCIF 16-bit: **pfc 1.73× beats CCSDS-121-class Rice (1.71×) by +1.5%** on the same MED predictor; within −2.7% of JPEG-LS.
- `make stress` — randomised property + edge + negative + fuzz under ASan/UBSan: **15 063 cases
  (5000 random round-trips across all codecs + adversarial edges + 150 000 decode-fuzz), 0 failures**.
  *This run found and fixed a real defect:* `pfc_bound()` under-estimated the per-block framing
  overhead for skinny incompressible images (e.g. 1×30000 8-bit), so a caller sizing to `pfc_bound`
  could get a spurious `PFC_E_BOUND`. Fixed to account for the worst-case block count; re-verified.
- `make misra` — `cppcheck --addon=misra` gate (CI; cppcheck absent in this env).
- `fuzz_pfc.c` — libFuzzer harness (CI; clang absent in this env).

### Structural coverage (gcov, full corpus = test_pfc + stress)

| File | Lines | Branches taken |
|------|-------|----------------|
| pfc_arith.c (range coder) | 100% | 95% |
| pfc_model.c | 100% | 100% |
| pfc_crc.c / pfc_frame.c | 100% | 100% |
| pfc.c (dispatch) | 98.6% | 84% |
| pfc_image.c | 98.8% | 93% |
| pfc_seq.c | 97.2% | 86% |
| pfc_columnar.c | 96.8% | 88% |

Driving coverage exposed that the `seq`/`float`/`columnar` **error-containment paths had no direct
tests** (only `image` did); targeted bit-flip + truncation + malformed-header + invalid-param tests
were added for every codec (`test_validation`, `test_corruption_all`, `test_more_guards`). The
residual ~9 uncovered lines are **defensive guards verified by inspection**: (a) internal
re-validation the dispatcher already enforces; (b) the raw-block plen-mismatch repair, reachable
only with a CRC-valid-but-length-inconsistent block (negligible from real corruption) and
structurally identical to the tested CRC-mismatch repair. MC/DC measurement is a future step.

See `docs/mission-safety.md` for the gap between this evidence and formal flight qualification.

## Open items (toolchain-gated / future)

- **Big-endian hardware run** of the test suite (validates R4 end-to-end) — needs a cross toolchain
  + qemu (e.g. `powerpc-linux-gnu-gcc` + `qemu-ppc`), absent here. Cross-build invocation is
  documented in the Makefile/README; the wire format is already proven canonical.
- **MISRA report** (`cppcheck --addon=misra`) and **libFuzzer run** — harnesses wired; tools absent here.
- **Richer context** (JPEG-LS bias correction / sign-aware gradients) to close the remaining −2.7%
  vs JPEG-LS and approach the research-measured pertype wins.
- **CCSDS-123** predictive comparison (full hyperspectral standard) beyond the CCSDS-121 baseline.
