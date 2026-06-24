# pertype-flight (`libpfc`) — design spec

**Date:** 2026-06-24
**Status:** approved, building
**Branch:** `flight-core` (local only; do not push to GitHub until told)

## Goal

A NASA-deployable, lossless flight compression core derived from pertype: a C99 library
(`libpfc`) that runs the **encoder on a spacecraft** (radiation-hardened CPU, no OS guarantees,
no dynamic memory) and the **decoder on the ground**, byte-exact, and is cross-checked against the
existing Python/Rust pertype reference so NASA can decode with whichever implementation they trust.

The asymmetry is the spine: one C core compiles for both the probe (encode) and the ground
(decode), and the *same* core is the bit-exactness oracle.

## Fixed decisions (from brainstorming)

| Axis | Decision |
|------|----------|
| Language/form | **C99** to JPL *Power of Ten* / **MISRA-C:2012**; build on the existing `_native` C |
| Codec scope | **Broad** — 4 predictor front-ends (image 2-D, 1-D sequence, float byte-plane, columnar) sharing one entropy backend + one framing layer |
| Definition of done | **Full flight-qualification artifacts** — flight-credible core + requirements/traceability + MISRA static analysis + fault-injection tests + CCSDS comparison |
| Licensing | **Permissive (Apache-2.0)** flight core in `flight/`; main pertype lib stays AGPL (sole copyright holder may relicense own algorithms) |

## Non-negotiable properties (the requirements the core must satisfy)

- **R1 Lossless** — `decode(encode(x)) == x` byte-for-byte, all inputs.
- **R2 No dynamic allocation** — no `malloc`/recursion after init; all working memory is a
  caller-supplied context of compile-time-known size (`PFC_WORKMEM_BYTES`).
- **R3 Bounded memory** — working set bounded by compile-time maxima (`PFC_MAX_COLS`, band height).
- **R4 Deterministic** — integer-only, no floating point; endianness-neutral canonical stream so a
  big-endian RAD750 encoder and a little-endian RISC-V/LEON/x86 ground decoder interoperate.
- **R5 No expansion** — `pfc_bound(codec, n)` is a hard ceiling; incompressible data falls back to
  store-raw per block.
- **R6 Error containment** — every block is independently decodable with a CRC-32; a corrupted
  downlink frame loses exactly one block, never crashes, never reads out of bounds.
- **R7 Bit-exact reference** — C core round-trips byte-identically with the Python/Rust pertype
  reference on a real corpus.

## Architecture

New top-level `flight/` directory:

```
flight/
  LICENSE                  Apache-2.0
  include/pfc.h            public API: status codes, codec ids, pfc_encode/decode/bound, PFC_* limits
  src/pfc_arith.[ch]       integer range coder (entropy backend) — Subbotin carryless, 32-bit
  src/pfc_model.[ch]       adaptive magnitude-category model (small alphabet, (f+1)>>1 rescale)
  src/pfc_resid.[ch]       residual <-> (category k + mantissa bits) mapping (Exp-Golomb/JPEG style)
  src/pfc_image.[ch]       2-D MED predictor front-end (slice 1)
  src/pfc_seq.[ch]         1-D delta/fixed/LMS front-end          (phase 2)
  src/pfc_float.[ch]       float byte-plane split front-end       (phase 2)
  src/pfc_columnar.[ch]    de-interleave front-end                (phase 2)
  src/pfc_frame.[ch]       block framing: magic|ver|codec|params|orig-len|{blocks: len|flags|CRC32|payload}
  src/pfc.[ch]             top-level encode/decode dispatch
  src/pfc_crc.[ch]         CRC-32 (table-free or static table)
  test/                    host tests: round-trip, bound, fault-injection, cross-check vs reference
  docs/requirements.md     numbered requirements + traceability matrix
  Makefile                 host build (gcc/clang) + cross-compile notes (BE PowerPC, SPARC/LEON, RISC-V)
  README.md
```

### Entropy backend (`pfc_arith` + `pfc_model` + `pfc_resid`)

- **Range coder**: classic 32-bit carryless range coder (`low`, `range`, byte renorm with
  `TOP=1<<24`, `BOT=1<<16`). `enc(cum,freq,tot)` / `dec_getfreq(tot)`+`dec_update(cum,freq,tot)`.
  Equiprobable "bypass" bits for mantissa via `tot=2`.
- **Residual coding** (reversible, exact): zigzag signed→unsigned `u`; `k = bitlength(u)`;
  entropy-code `k` with the adaptive model (alphabet `0..PFC_KMAX`); if `k>1` emit the low `k-1`
  mantissa bits bypass. Decode mirrors. (`u=0→k=0`; else top bit implied, `u=(1<<(k-1))|low`.)
- **Model**: per-context frequency array over `k`, linear cumulative (small alphabet), rescale at a
  total threshold. Context = neighbour magnitude class (image) — bounded table.

### Image front-end (`pfc_image`, slice 1)

- MED/LOCO-I predictor on uint16 (or uint8) plane: `pred = MED(left, up, upleft)`; residual =
  value − pred; the band's first row/col use the mid-value default so each **band is independent**.
- The plane is split into horizontal **bands** (height = compile-time const). Each band is one
  framing block → independent predictor + entropy state → satisfies R6.

### Framing (`pfc_frame`)

- Stream header: `PFC1` | version | codec id | codec params (e.g. width,height,bitdepth) | n_blocks.
- Block: `block_len(4) | flags(1) | crc32(4) | payload`. `flags.bit0` = store-raw (R5 fallback).
- Decode: per block verify CRC; on mismatch, mark the block (caller-visible status) and continue
  (R6). Store-raw path copies bytes verbatim.

### Public API (`pfc.h`) — buffer-in/buffer-out, caller owns all memory

```c
typedef enum { PFC_OK=0, PFC_E_PARAM, PFC_E_BOUND, PFC_E_CORRUPT, PFC_E_UNSUPPORTED } pfc_status;
typedef enum { PFC_CODEC_IMAGE16=1, PFC_CODEC_SEQ=2, PFC_CODEC_FLOAT=3, PFC_CODEC_COLUMNAR=4 } pfc_codec;

size_t pfc_bound(pfc_codec c, size_t n_in);          /* worst-case output size (R5) */
pfc_status pfc_encode(pfc_codec c, const pfc_params*, const void* src, size_t n,
                      void* dst, size_t cap, size_t* out, pfc_ctx* work);
pfc_status pfc_decode(const void* src, size_t n, void* dst, size_t cap, size_t* out, pfc_ctx* work);
```

`pfc_ctx work` is a caller-provided struct of size `PFC_WORKMEM_BYTES` (R2/R3).

## Build plan (vertical slice first)

1. **Slice 1 (this build):** `pfc_arith` + `pfc_model` + `pfc_resid` + `pfc_image` (MED) +
   `pfc_frame` + `pfc.c` + `pfc_crc`. Host Makefile. Tests: round-trip on synthetic + real 16-bit
   image, bound, store-raw, fault-injection. Requirements doc + traceability skeleton.
2. **Phase 2:** add `pfc_seq`, `pfc_float`, `pfc_columnar` on the same backend/framing.
3. **Phase 3:** bit-exact cross-check vs Python/Rust reference (R7); MISRA `cppcheck` in CI;
   ASan/UBSan + fuzz; CCSDS-121/123 comparison; cross-compile validation.

## Testing & qualification (the "full qual" choice)

- Host test suite: round-trip (R1), bound/no-expansion (R5), determinism (R4), bit-flip
  fault-injection with bounds checks (R6), memory-bound asserts (R2/R3).
- `cppcheck --addon=misra` + clang ASan/UBSan + libFuzzer harness on the decoder (untrusted input).
- Requirements traceability matrix: each Rn → test id(s).
- CCSDS comparison: ratio + working-memory vs CCSDS-121 (Rice) and CCSDS-123 on instrument data.

## Out of scope (v1)

- FPGA/HDL implementation (high-rate imagers) — different effort.
- Lossy modes (CCSDS-122 territory).
- Actual mission V&V sign-off — we produce the artifacts that feed it, not the flight cert.
