# libpfc — requirements & traceability

Flight-software requirements for the pertype-flight lossless core, each with a verification method
and the test(s) that exercise it. IDs are stable; tests live in `flight/test/` and `flight/ground/`.

## Requirements

| ID | Requirement | Verification |
|----|-------------|--------------|
| **R1** | **Lossless.** For every valid input, `decode(encode(x)) == x` byte-for-byte. | Test (round-trip, all 5 codecs, synthetic + real) |
| **R2** | **No dynamic allocation.** No `malloc`/`free`/recursion. Working memory is a caller-supplied `pfc_ctx`. | Inspection (no alloc symbols in the `.so`) + test |
| **R3** | **Bounded memory.** Working set ≤ compile-time maxima; footprint = `pfc_workmem_bytes()`. | Inspection + test |
| **R4** | **Deterministic & portable.** Integer-only; canonical little-endian wire format (big-endian encoder ⇄ little-endian decoder). | Inspection (no FP in `src/`) + independent LE decoder + real big-endian execution |
| **R5** | **No expansion.** Output never exceeds `pfc_bound()`; incompressible blocks store raw. | Test (random data within bound, all codecs); formal proof attempted, did not converge |
| **R6** | **Error containment.** Each block independently CRC-protected; a corrupt/truncated frame is reported, never reads OOB or crashes — on any `size_t` width. ⚠️ **"loses one block" holds for IMAGE/SEQ/COLUMNAR/FLOAT but NOT for SPECTRAL**, whose inter-band prediction propagates a single block loss across the rest of the cube (measured; mitigable via refresh bands — see §2.5.1). | Test (bit-flip + truncation) + ASan/UBSan + fuzz + formal proof (32-bit model) + per-codec containment measurement |
| **R7** | **Independent reference.** An independent implementation decodes the C encoder's output byte-for-byte. | Cross-check (pure-Python ground decoder vs C encoder) |
| **R8** | **Coding-standard compliance.** MISRA-C:2012 + JPL Power-of-Ten discipline (integer-only, no recursion, bounded loops, explicit casts). | Static analysis (`cppcheck --addon=misra`), triaged rule-by-rule |
| **R9** | **Structural coverage.** Line and branch coverage stay above a floor set below the measured baseline, every push. | Coverage-instrumented test run (gcov/gcovr) |
| **R10** | **Bounded stack.** Worst-case stack depth is statically bounded and known, on the flight target ABI as well as the host. | Call-graph longest-path over `-fstack-usage` frames |

R8/R9/R10 are process/assurance requirements, not functional ones — added here so every automated
CI gate has a requirement ID to trace back to, not just the functional/safety properties R1–R7.

## Codec coverage (broad scope)

| Codec | Front-end | Status |
|-------|-----------|--------|
| `PFC_CODEC_IMAGE` | 2-D MED/LOCO-I predictor + bias correction + gradient context + run mode | ✅ |
| `PFC_CODEC_SEQ` | 1-D order-1 delta (int8/16/32, signed/unsigned) | ✅ |
| `PFC_CODEC_FLOAT` | float32/64 byte-plane split | ✅ |
| `PFC_CODEC_COLUMNAR` | record de-interleave + per-plane delta | ✅ |
| `PFC_CODEC_SPECTRAL` | multi/hyperspectral cube: inter-band MED-of-difference prediction | ✅ |

All five share one integer range coder, one adaptive category model, and one block-framing layer.

## Traceability matrix

Requirement → design (the module/function that implements it) → verification procedure → result,
per §2.1 of `mission-safety.md`'s qualification gap analysis. This is a **solo-authored** matrix,
not an independently audited one — review records, where they exist, are the detailed per-finding
writeups already in this repo (this file's CI sections below, `misra-deviations.md`), linked from
the Result column; that's real self-review evidence but not a substitute for IV&V (tracked
separately, out of scope for now — see `mission-safety.md` §2.1).

| ID | Design (implementing module) | Verification procedure | Result / evidence |
|----|-------------------------------|-------------------------|--------------------|
| R1 | Per-codec encode/decode pairs (`pfc_image.c`, `pfc_seq.c`, `pfc_columnar.c` [also FLOAT], `pfc_spectral.c`) over the shared range coder (`pfc_arith.c`) + adaptive model (`pfc_model.c`) + block framing (`pfc_frame.c`, `pfc_crc.c`), dispatched by `pfc_encode`/`pfc_decode` (`pfc.c`) | `test_pfc.c`: `roundtrip()`/`check_rt()` drive image (gradient/random/flat, 8/16-bit, odd sizes) + `test_seq`/`test_float`/`test_columnar`/`test_spectral`; `stress.c` adds 5 000 randomised round-trips across all codecs; `bench_real.py` round-trips real CyCIF | ✅ 146 unit + 15 063 stress cases, 0 failures; real CyCIF 4/4 byte-exact. **Exhaustive small-input CBMC proof attempted (SEQ, count=1), did not converge** — see the CBMC section below; covered by testing only, not formal proof |
| R2 | `pfc_ctx` is a plain caller-owned struct (`pfc_internal.h`); no `malloc`/`free`/`stdlib.h` anywhere in `src/` | Inspection: grep `src/` for allocator calls; `nm -uD build/libpfc.so` | ✅ zero alloc symbols imported |
| R3 | `pfc_workmem_bytes()` = `sizeof(struct pfc_ctx)` (`pfc.c`); size tunable via `-DPFC_MAX_COLS`/`-DPFC_BAND_ROWS` (`pfc.h`) | Inspection of the struct layout + printed at test startup | ✅ 329 096 B at compile-time defaults |
| R4 | Explicit little-endian serialisation helpers `pfc_put_u32`/`pfc_get_u32` (`pfc_internal.h`); no `float`/`double` anywhere in `src/` | Inspection (no FP types); `ground/pfc_decode.py` (independent explicit-LE decoder) via `test_crosscheck.py`; `bigendian` CI job: full test suite under `qemu-ppc` (real BE *execution*) + `emit.c` LE-vs-BE byte-comparison | ✅ stream verified; ✅ **real BE execution confirmed** — all 139 checks green under emulated PowerPC, LE/BE `emit.c` output byte-identical |
| R5 | `pfc_bound()` closed-form worst-case formula (`pfc.c`); every codec encoder's store-raw fallback when the coded form would exceed capacity | `test_pfc.c`/`stress.c`: random inputs (all codecs) asserted `≤ pfc_bound`, store-raw path exercised | ✅ verified by test (0 failures, 2 real `pfc_bound` under-estimate bugs found+fixed by this same test in an earlier session). **CBMC sufficiency proof attempted (SEQ, count=1), did not converge** — see CBMC section below |
| R6 | Per-block CRC framing, `pfc_block_write`/`pfc_block_read` (`pfc_frame.c`) + `pfc_crc32` (`pfc_crc.c`); decode repairs (zero-fills) a CRC-mismatched or truncated block instead of propagating it | `test_pfc.c::test_fault_injection`/`::test_truncation`/`::test_corruption_all`/`::test_more_guards`; `make asan` (ASan+UBSan); `fuzz_decode.py` (Python harness, 20 000 iterations); `libfuzzer` CI job (real coverage-guided C fuzzing); **CBMC proofs** of `pfc_block_read` AND `pfc_block_write` (`proofs/cbmc/`, `cbmc` CI job, 32-bit `size_t` model) | ✅ 0 crashes/OOB across all test-based evidence; **libFuzzer found and fixed 2 real heap-buffer-overflows** (SPECTRAL header-size gap, COLUMNAR unbounded `block_recs`) within minutes of first running; **CBMC formally proved** (not sampled) that neither `pfc_block_read` (`0 of 165 failed`) nor `pfc_block_write` (`0 of 152 failed`) accesses out of bounds under a 32-bit model — and authoring the read-side proof found the real `size_t`-wraparound bug it now guards against |
| R11 | SEU tolerance: per-band model resets + small independently-framed blocks are intended to bound the blast radius of an encoder-side upset (`pfc_model_reset` per block, `pfc_frame.c` framing) | `make seu` (`test/seu_inject.c`): flips 1..N bits in `pfc_ctx` mid-encode via linker `--wrap`, classifies the outcome, and measures how far the damage spreads. Run across all five codecs, with two sampling modes: uniform (orbit rate) and stratified (per-region conditional risk); `SEU_BURST=N` adds adjacent-bit burst upset | ⚠️ **PARTIALLY MET — see the SEU section below.** The CRC detected **zero** encoder-side upsets on any codec, so silent corruption is the realistic failure mode. Containment holds for IMAGE/SEQ/COLUMNAR/FLOAT but **fails for SPECTRAL** (12 of 15 corruptions crossed a block boundary) because inter-band prediction makes its blocks non-independently-decodable. Also found and fixed a real SEU-induced infinite loop in the range coder |
| R7 | `ground/pfc_decode.py`, a from-scratch pure-Python decoder implementing the same explicit-LE wire format independently of the C encoder | `test_crosscheck.py`: C-encode → Python-decode, compared byte-for-byte to the original | ✅ **10/10** byte-exact, incl. real CyCIF, real AVIRIS hyperspectral, flat run-mode, all codecs |
| R8 | All of `src/` written to MISRA-C:2012 / JPL Power-of-Ten discipline (integer-only, no recursion, bounded loops, explicit widening casts) | `misra` CI job: `cppcheck --addon=misra` against `.cppcheck-suppressions`; full rule-by-rule triage recorded in `misra-deviations.md` | ✅ 179 findings on first run (gate works); triaged — 4 real + fixed, 27 tool-limitation false positives, 148 deliberate verified-safe deviations; confirmed green in CI with suppressions applied |
| R9 | N/A (process requirement, not a design property) | `coverage` CI job: `make coverage` (gcov/gcovr) over the `test_pfc` + `stress` corpus, gated at 95% line / 80% branch | ✅ 98.4% lines / 89.3% branches project-wide, gated in CI below that baseline. **MC/DC not measured** — open gap, see `mission-safety.md` §2.2 |
| R10 | No recursion and no function pointers anywhere in `src/` (JPL Power-of-Ten discipline) — which is precisely what makes the bound exact rather than estimated | `stackdepth` CI job: `make stackdepth` (host ABI) + `make stackdepth-ppc` (flight target ABI) via `tools/stack_depth.py`, gated at `STACK_BUDGET` = 1024 B | ✅ **464 B worst case on the flight target** (PowerPC BE, `pfc_decode`), 632 B on x86-64. Both under budget. The tool asserts acyclicity and call-graph completeness, so it fails loudly rather than reporting a wrong number if recursion or a callback is ever introduced |

## Verification environment (this build)

- `make strict` — `-std=c99 -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wstrict-prototypes -Werror`: **139/139, zero warnings**.
- `make asan` — ASan + UBSan: **139/139, no diagnostics**.
- `test_crosscheck.py` — C encoder vs independent Python decoder: **10/10 byte-exact** (incl. real CyCIF).
- `fuzz_decode.py` — **20 000 iterations**, decoder survived all random/mutated input.
- `ccsds_compare.py` — real CyCIF 16-bit: **pfc 1.76× beats CCSDS-121-class Rice (1.71×) by +2.8%** on the same MED predictor; within **−1.3%** of JPEG-LS at the default band (−0.5% at band=64).

### Context-adaptation study (closing the JPEG-LS gap)

Sweeping band size (the `-DPFC_BAND_ROWS` knob) on real CyCIF separated two effects:

| band | workmem (MAX_COLS=8192) | vs JPEG-LS |
|------|-------------------------|------------|
| 16 (default) | 322 KB | −1.3% |
| 32 | 578 KB | −0.9% |
| 64 | 1.0 MB | −0.5% |
| whole-image | 17 MB | ≈ −0.3% |

Findings: (1) **per-band model resets** (needed for error-containment) cost ~1% via adaptation
dilution — recoverable by larger bands, a memory/containment tradeoff (with instrument-matched
`MAX_COLS` the cost is far lower, e.g. ~160 KB for a 1280-wide sensor at band 64). (2) **Modelling
the top mantissa bit** adaptively per category is a general win (−0.3 to −0.4% here, and large on
prediction-residual codecs: seq-ramp 15×→261×, columnar 11×→14×). (3) A directional *entropy*
context **regressed** on photon-noisy data (dilution, no directional payoff) and was dropped — the
residual sub-percent floor is finer context modelling that only pays off with bigger bands.
- `make stress` — randomised property + edge + negative + fuzz under ASan/UBSan: **15 063 cases
  (5000 random round-trips across all codecs + adversarial edges + 150 000 decode-fuzz), 0 failures**.
  *This run found and fixed a real defect:* `pfc_bound()` under-estimated the per-block framing
  overhead for skinny incompressible images (e.g. 1×30000 8-bit), so a caller sizing to `pfc_bound`
  could get a spurious `PFC_E_BOUND`. Fixed to account for the worst-case block count; re-verified.
- `make misra` — `cppcheck --addon=misra` gate. Not run locally (cppcheck absent, and a from-source
  build was deliberately abandoned mid-run after it contributed to a machine crash — see the
  `misra` job in `flight-ci.yml`, which installs cppcheck from the Ubuntu archive instead of
  building it). **Authored, not yet executed.**
- `fuzz_pfc.c` — libFuzzer harness. Not run locally (no clang in this environment). Wired into the
  `libfuzzer` job in `flight-ci.yml` (bounded to 120s wall-clock). **Authored, not yet executed.**

### Structural coverage (gcov/gcovr, full corpus = test_pfc + stress)

**Now automated as a CI gate** (`make coverage`, `coverage` job in `flight-ci.yml`) rather than a
one-off manual measurement — every push re-runs it and fails below 95% line / 80% branch
project-wide (set under the measured baseline to leave headroom for legitimate new code). Current
measured baseline, same corpus as `native`:

| File | Branches taken |
|------|-----------------|
| pfc_crc.c | 100% |
| pfc_model.c | 100% |
| pfc_arith.c (range coder) | 95% |
| pfc_frame.c | 87% |
| pfc_image.c (incl. bias + run mode) | 93% |
| pfc_seq.c | 88% |
| pfc.c (dispatch) | 85% |
| pfc_columnar.c | 85% |
| pfc_spectral.c | 84% |

Project-wide: **98.4% lines** (908/923), **89.3% branches** (553/619), 100% functions.

Driving coverage exposed that the `seq`/`float`/`columnar` **error-containment paths had no direct
tests** (only `image` did); targeted bit-flip + truncation + malformed-header + invalid-param tests
were added for every codec (`test_validation`, `test_corruption_all`, `test_more_guards`). The
residual uncovered lines are **defensive guards verified by inspection**: (a) internal
re-validation the dispatcher already enforces; (b) the raw-block plen-mismatch repair, reachable
only with a CRC-valid-but-length-inconsistent block (negligible from real corruption) and
structurally identical to the tested CRC-mismatch repair.

### MC/DC coverage (R9) — measured, and it is the weakest structural number here

gcov cannot measure MC/DC at all, so it stayed an open gap until clang 18's `-fcoverage-mcdc`
(`make mcdc`, `tools/mcdc_gate.py`, `mcdc` CI job). The first measurement is worth stating plainly
because it undercuts the comfortable reading of the numbers above:

| metric | coverage |
|--------|----------|
| lines | 98.4% |
| branches | 89.4% |
| **MC/DC conditions** | **55.65%** (64 of 115) at first measurement → **76.52%** (88 of 115) after `test_mcdc.c` |

That is not a contradiction; it is precisely what MC/DC exists to expose. A decision like
`(a == NULL) || (b == NULL) || (c == NULL)` reaches both outcomes — 100% branch — as soon as one
test passes all-valid and one passes a single NULL. Nothing about that demonstrates the other two
conditions are load-bearing: invert one of them and both tests still pass. MC/DC requires showing
each condition *independently* flips the result, which needs N+1 vectors for an N-way chain.

`test/test_mcdc.c` was added to close these, with vectors chosen for condition independence rather
than behaviour alone. These are real robustness tests, not metric-chasing: the magic-byte cases
would catch a mistyped index (`s[2]` checked twice, leaving one byte unvalidated) that every
existing behavioural test misses, because those only ever corrupt the whole header at once; and
several of the wire-header clauses guard *liveness*, not just validity — a zero
`block`/`block_recs`/`band` field would leave the corresponding decode loop unable to advance.
A second pass (`G3.4`) extended `test_mcdc.c` to cover the remaining header/parameter validation and
store-raw conditions, and to document the rest.

| file | before | after first pass | after G3.4 |
|------|--------|------------------|------------|
| `pfc.c` | 45.0% | **100%** (20/20) | 100% |
| `pfc_frame.c` | 50.0% | **100%** (4/4) | 100% |
| `pfc_model.c` | 100% | 100% (2/2) | 100% |
| `pfc_seq.c` | 66.7% | **80.0%** (12/15) | improved |
| `pfc_image.c` | 75.0% | 75.0% (24/32) | improved |
| `pfc_arith.c` | 66.7% | 66.7% (4/6) | unchanged |
| `pfc_columnar.c` | 36.4% | **63.6%** (7/11) | improved |
| `pfc_spectral.c` | 34.8% | **60.9%** (14/23) | improved |
| `pfc_internal.h` | 50.0% | 50.0% (1/2) | unchanged |

#### G3.4a — parameter and header validation (14 conditions)

Added targeted tests in `test_mcdc.c` for the encode-side guards the dispatcher does not already
cover and for the one decode-side header chain that was missing:
- `pfc_spectral.c:199` SPECTRAL encode guard (bad bitdepth / width==0 / height==0 / count==0 /
  width>PFC_MAX_COLS).
- `pfc_image.c:309` IMAGE encode guard (bad bitdepth / width==0 / height==0).
- `pfc_columnar.c:25` COLUMNAR encode guard (rw==0 / rw>PFC_BLOCK_BYTES / cnt==0).
- `pfc_seq.c:73` SEQ encode guard (count==0). The `block==0` half is unreachable on a 64-bit model
  because the dispatcher restricts `elem` to {1,2,4}, so `PFC_BLOCK_BYTES/elem` is always >0; it
  is classified as unreachable in G3.4b rather than missing coverage.
- `pfc_image.c:383` IMAGE decode header guard (width==0 / height==0 / band==0 /
  width>PFC_MAX_COLS).

#### G3.4b — `pfc_size_mul` capacity guards (8 conditions)

These guards are defensive 32-bit protections on a 64-bit host. On the 64-bit CI machine model they
are **unreachable by construction**:
- `pfc_image.c:391` `width*height*es`: `width <= PFC_MAX_COLS` (8 192) and `es <= 2`, so the
  product cannot overflow 64-bit `size_t`.
- `pfc_columnar.c:110` `rw*cnt`: `rw <= PFC_BLOCK_BYTES` and `cnt` is bounded by the caller's
  `src_len`, so the product cannot overflow 64-bit `size_t`.
- `pfc_seq.c:144` `count*elem`: `elem in {1,2,4}` and `count` is bounded by `src_len`, so the
  product cannot overflow 64-bit `size_t`.
- `pfc_internal.h:183` the `pfc_size_mul` guard itself: a hand-verified CERT C INT30-C idiom;
  CBMC over the unconstrained domain did not converge and is not worth an unreliable CI gate.
- `pfc_spectral.c:277` is the **only reachable one**: four untrusted factors up to 78 bits can
  overflow even 64-bit `size_t`; it has a dedicated regression test (`test_spectral_corruption`).

A 32-bit MC/DC build would be needed to exercise the unreachable-on-64-bit branches; that build is
not currently part of CI. The unreachable classification is recorded here rather than left implicit.

#### G3.4c — store-raw fallback (2 conditions)

Added `mcdc_store_raw()` forcing both `(e.overflow != 0) || (e.pos >= raw_bytes)` conditions:
- incompressible random data exercises `e.pos >= raw_bytes` with `e.overflow == 0`;
- a tiny output capacity exercises `e.overflow == 1` before the store-raw decision.

#### G3.4d — genuinely state-dependent conditions (3 conditions)

- `pfc_image.c:121` gradient sign tie-break: covered by a crafted image where `q1==0`, `q2==0`,
  `q3<0` (`mcdc_image_gradient_tiebreak()`).
- `pfc_arith.c:39` / `:118` range-coder renorm underflow branch: **not unit-testable with the
  public API**. The branch is reached only when `range < PFC_RC_BOT` while `low` and `low+range`
  straddle a `PFC_RC_TOP` boundary — a state that depends on the exact low/range relationship built
  up over many prior symbols. It is covered indirectly by the `test_renorm_bound` regression (which
  verifies the bounded loop terminates under corrupted state), but that does not exercise the
  condition's independent effect for MC/DC. Recorded as not unit-testable; reaching it would need
  a solver-assisted search or a hand-built range-coder state fixture.

**The gate is a ratchet, not a compliance claim.** `MCDC_MIN` sits just under the current
measurement so regressions fail the build, and is raised as conditions get covered. DO-178C wants
~100% MC/DC on decision-heavy safety-critical code; this is a long way from that, and a passing
`make mcdc` must not be read as "MC/DC compliant". See `mission-safety.md` §2.2.

### Worst-case stack depth (R10) — `tools/stack_depth.py`

Flight software must bound maximum stack usage. For libpfc that bound is **exact, not estimated**,
because the two properties that make the general problem undecidable are both already forbidden by
the project's JPL Power-of-Ten discipline — and the tool *asserts* them rather than assuming:

- **No recursion** → the call graph is a DAG, so worst-case depth is a terminating longest-path
  problem. If a cycle ever appears the tool refuses to report a number instead of looping or
  guessing.
- **No function pointers** → every call edge is statically resolvable from the disassembly, so the
  graph is complete. An indirect call would force "could reach any address-taken function", making
  any bound useless; the tool treats finding one as a hard error.

Method: GCC `-fstack-usage` gives per-function frame sizes; `objdump -dr` gives the call edges;
the tool computes the maximum sum along any path from each public entry point.

| Target | `pfc_encode` | `pfc_decode` | Worst case |
|--------|--------------|--------------|------------|
| x86-64 (host, `-O2`) | 560 B | 632 B | **632 B** |
| **PowerPC BE 32-bit (flight target ABI, `-O2`)** | 384 B | **464 B** | **464 B** |

The deepest path is the same shape on both targets — `pfc_decode → pfc_image_decode →
pfc_image_decode_band → pfc_resid_decode → pfc_uint_decode → pfc_rc_decode_bits →
pfc_rc_dec_renorm` — i.e. the image codec's per-sample decode chain, not any framing or dispatch
path. CI gates both at `STACK_BUDGET` = 1024 B, comfortably above both so it catches a structural
regression (a new deep call chain) rather than ordinary codegen drift.

**Caveats, stated rather than buried** — this is a flight artifact, so its limits matter:
1. `.su` frame sizes exclude the return address pushed by `call`, and any red-zone use. Treat the
   figures as a tight lower bound and apply margin; the tool prints a suggested 100% margin.
2. One `memset` edge into libc has no `.su` entry and is reported as unresolved-external rather
   than silently counted as zero.
3. GCC labels `pfc_encode`/`pfc_decode`'s own frames `dynamic,bounded` on x86-64 (but `static` on
   PowerPC), so on the host those two frames are GCC's own bound rather than a fixed size — another
   reason the flight-target number, not the host number, is the one to quote.

### Downlink containment (R6) — `test/downlink_containment.c`

R6 says a corrupt frame "loses one block". `make containment` tests that directly: flip **one bit
in one block's payload** (failing only that block's CRC) and count which bands differ.

| codec | bands damaged (of 6) | verdict |
|-------|----------------------|---------|
| IMAGE (control) | 1 | contained |
| **SPECTRAL** | **6** | **propagated** |

The IMAGE control is what makes the result attributable — identical corruption, geometry and block
size; the only difference is inter-band prediction. **R6 is therefore wrong as written for
SPECTRAL.** It is not a silent failure (decode still returns `PFC_E_CORRUPT`), and it is inherent
to the inter-band prediction that gives SPECTRAL its compression advantage — containment and ratio
are in direct tension for this codec.

**Mitigation, implemented, default-off:** `pfc_params::elem` sets an inter-band refresh interval N;
every N'th band is coded spatially-only, bounding propagation to N bands. Carried in the stream
header (previously-reserved byte 7) so streams remain self-describing — the independent Python
ground decoder honours it too (R7 case `spectral-refresh4`). `refresh=0` is **byte-identical** to
the pre-feature encoder (asserted in `test_spectral_refresh`), so nothing changes for existing
callers.

**The cost is data-dependent.** On a strongly inter-band-correlated 12-band synthetic cube,
refresh=4 bounds damage to 4 of 12 bands but costs **+14.68%**; refresh=6 costs +7.30%. (An earlier
figure of +0.88% was measured on a weakly-correlated cube where the inter-band predictor was barely
working — misleading, and discarded.) On **real AVIRIS Indian Pines** (200 bands, 145×145, uint16),
the same intervals are much cheaper: refresh=4 costs **+3.96%**, refresh=6 **+2.60%**, refresh=8
**+1.98%**, and refresh=10 only **+1.44%** (see `mission-safety.md` §2.5.1 for the full curve).
Pick an interval that divides the band count evenly: refresh=6 dominates refresh=8 on the 12-band
synthetic cube (same cost, tighter bound), while on the 200-band real scene refresh=8 (divisor) is
cheaper than refresh=6 (non-divisor).

### SEU fault injection (R11) — `test/seu_inject.c`

§2.5 of `mission-safety.md` used to *assert* two things about single-event upsets in the encoder's
working memory. `make seu` now measures them. It flips one bit in `pfc_ctx` mid-encode and observes
what reaches the ground.

Injection uses the linker's `--wrap` on `pfc_resid_encode`, so `src/` compiles **completely
unmodified** — no test-only `#ifdef` hooks inside MISRA-reviewed flight code. Every codec calls
that function once per sample, giving single-sample injection granularity.

Result over 7 200 uniform-random trials (64×64 16-bit image, 4 bands):

| outcome | count | |
|---------|-------|---|
| CLEAN (no observable effect) | 7 191 | 99.9% |
| **DETECTED (CRC/status caught it)** | **0** | **0.0%** |
| SILENT (decode said OK, data wrong) | 9 | 0.1% |

**The headline is the zero.** Not one encoder-side upset was detected by any mechanism in the
system. That is not a bug — it is the direct, measured consequence of what §2.5 already said in
prose: the CRC is computed *after* the corruption, over the already-wrong payload, so a corrupted
block is perfectly self-consistent. What the measurement adds is that this is the *only* outcome —
there is no incidental detection to fall back on. Encoder-side SEU is a **silent** failure mode,
end to end.

Two further findings the prose did not contain:

1. **Containment holds for four codecs and FAILS for SPECTRAL.** Measured per codec (each input
   sized to span several blocks, so the property is actually testable):

   | codec | silent corruptions | crossed a block boundary | worst damage |
   |-------|--------------------|--------------------------|--------------|
   | IMAGE 64×64@16 | 157 | **0** | 2 035 B of a 2 048 B block |
   | SEQ 65536×i16 | 87 | **0** | 65 002 B of a 65 536 B block |
   | COLUMNAR 16384×8B | 134 | **0** | 64 909 B of a 65 536 B block |
   | FLOAT 32768×f32 | 165 | **0** | 63 074 B of a 65 536 B block |
   | **SPECTRAL 32×32×4@16** | 15 | **12** ⚠ | 3 734 B vs a 1 024 B block (~3.6 blocks) |

   SPECTRAL's failure is architectural, not a coding error: it reconstructs band *z* by reading
   band *z−1* from the output buffer (`pfc_spectral.c`, `bzp`/`has_prev`), because inter-band
   MED-of-difference prediction is exactly where its compression advantage comes from. Its blocks
   are independently *framed and CRC'd* but **not independently decodable**, so a silently-wrong
   band feeds the next band's prediction. Containment and the codec's compression win are in direct
   tension. The same propagation was later confirmed for ordinary **downlink** corruption in
   `mission-safety.md` §2.5.1.

   The harness also supports multi-bit / burst upset via `make seu SEU_BURST=N`. An 8-bit burst run
   (2 000 trials for IMAGE, 250 for the others) produced the same qualitative containment picture:
   IMAGE/SEQ/COLUMNAR/FLOAT stayed contained; SPECTRAL still propagated when the model regions were
   hit. Burst upsets raised the conditional silent-corruption rates in the small model regions
   slightly, but did not change the containment conclusion.

   Where containment does hold, note that "contained" is not "small": for SEQ and COLUMNAR a block
   is 64 KB, so one upset can silently corrupt up to half a 128 KB payload.
2. **Risk is inverse to region size — which is exactly what makes hardening affordable.** Uniform
   sampling barely reaches the small model regions (they are <1% of the workmem), so the harness
   runs a second **stratified** pass with equal trials per region. At 300 trials each:

   | region | size | share of `pfc_ctx` | silent rate (conditional) |
   |--------|------|--------------------|---------------------------|
   | `tot[]` (context totals) | 144 B | 0.04% | **20.33%** |
   | `mant[][]` (mantissa model) | 132 B | 0.04% | **19.67%** |
   | `freq[][]` (category model) | 2 376 B | 0.72% | 7.67% |
   | `bias_*[]` (image bias) | 360 B | 0.11% | 7.00% |
   | `scratch[]` (block payload) | 262 160 B | 79.27% | 0.67% |
   | `xform[]` (de-interleave) | 65 536 B | 19.82% | 0.00% |

   **The actionable number: the four model regions total 3 012 bytes — 0.91% of the 330 KB
   workmem — and carry essentially all of the risk.** Protecting ~3 KB with EDAC, scrubbing, or a
   periodic checksum addresses the dominant failure mode; protecting all 330 KB would be
   ~100× the cost for almost no additional benefit. `scratch[]`/`xform[]` are large but nearly
   harmless because they are overwritten before use.

   These are *conditional* risks, not orbit rates — weight by the size column before drawing any
   mission-level conclusion. (Multiply through and the model regions still dominate: 0.91% of the
   area at ~10–20% severity versus 99% of the area at ≤0.67%.)

`make seu` is an on-demand analysis tool, **not** a CI gate — it takes minutes and its output is a
measurement rather than a pass/fail. Trial count is `SEU_TRIALS`; burst width is `SEU_BURST`
(default 1 bit).

See `docs/mission-safety.md` for the gap between this evidence and formal flight qualification.

## CCSDS-123 comparison (hyperspectral) — `ccsds123_compare.py`

On real AVIRIS Indian Pines (200 contiguous bands, 145×145, 16-bit), with the entropy coder held
constant (block-adaptive Rice):

| predictor | ratio |
|-----------|-------|
| per-band MED (spatial only) | 2.03× |
| inter-band delta (naive spectral) | 1.88× — *worse* |
| CCSDS-123-class (spectral+spatial least-squares) | **2.37×** |

Real codecs: pfc per-band 2.03×, JPEG-LS 2.02×, **pfc SPECTRAL 2.35× (−1.2% vs CCSDS-123-class)**.

The `PFC_CODEC_SPECTRAL` codec **closed the gap from −16.9% to −1.2%** — near parity with the
CCSDS-123-class predictor — using a simple integer MED-of-difference predictor (predict the
inter-band difference image spatially) feeding the existing arithmetic + mantissa coder. Findings:
(1) on hyperspectral, exploiting inter-band correlation is the whole game; (2) **naive inter-band
delta hurts** (bands have a scale/offset) — but MED on the *difference image* captures the
gain-induced spatial structure that naive delta misses; (3) pfc's arithmetic coder ≈ Rice on this
data, so the predictor is everything. (The CCSDS-123-class bar is a per-band least-squares rendition
of the standard's adaptive spectral+spatial predictor — class-faithful, not bit-exact. Lossless
round-trip and C↔Python cross-check verified on real AVIRIS bands.)

## CI: automating the toolchain-gated gates — `flight-ci.yml`

The three items below all needed a toolchain (cppcheck, clang, a PowerPC cross-compiler + qemu)
absent from every environment this project has been developed in so far. Rather than install them
on a dev machine (a from-source cppcheck build was abandoned mid-build after contributing to a
machine crash), a CI workflow now runs all three on a fresh `ubuntu-latest` runner (has root +
apt, so prebuilt packages install cleanly — no from-source builds needed).

**Two copies, same content:** `.github/workflows/flight-ci.yml` (GitHub Actions) and
`.gitea/workflows/flight-ci.yml` (Gitea Actions). `flight-core`'s actual push target is `origin`
(the self-hosted gitea), not GitHub — GitHub only reads `.github/workflows/`, so without the
gitea copy the GitHub version would sit dormant on this branch. The gitea copy carries
**materially higher uncertainty**, flagged in its own header: whether Gitea Actions is even
enabled on this instance, whether a runner advertises the `ubuntu-latest` label used here, and
whether the runner has outbound internet access at all (needed for `apt-get` and to fetch
`actions/checkout`). If any of that is wrong, every job fails visibly at its first network step —
not silently. Keep the two files in sync by hand; job bodies are otherwise identical.

- **`native` job** — `make check` (the full local gate: strict, ASan/UBSan, R7 cross-check,
  decoder fuzz, 15k-case stress).
- **`misra` job** — `apt-get install cppcheck` (prebuilt binary, not a source build) → `make misra`.
- **`cbmc` job** — `apt-get install cbmc` → `make cbmc`: a bounded model-checking proof (not a
  test) that `pfc_block_read` — the shared block-framing primitive every codec's decoder calls to
  parse untrusted downlink bytes — never reads out of bounds. Run with `--32` so CBMC models a
  32-bit `size_t`, matching the real flight targets (RAD750/LEON/RISC-V-32) rather than the
  64-bit host every other job here runs under; see `proofs/cbmc/harness_block_read.c` and the
  "CBMC proof" section below for why that distinction is the entire point of this gate.
- **`libfuzzer` job** — `apt-get install clang`, builds `fuzz_pfc.c` per its own documented
  invocation, runs bounded to 300s wall-clock (raised from an initial 120s once the gate proved
  its value — see below). The corpus persists across CI runs via `actions/cache` (a `libfuzzer-
  corpus-<run_id>` key with `restore-keys` grabbing the latest prior one — the standard "ratchet"
  cache pattern), so fuzzing coverage compounds run over run instead of restarting from nothing
  each time. A crash artifact uploads automatically on failure for easy local reproduction.
- **`bigendian` job** — installs `gcc-powerpc-linux-gnu` + `qemu-user`, then (1) runs the full
  139-check `test_pfc` suite ON emulated big-endian PowerPC hardware (proves no endian-dependent
  bug in the codec internals — genuinely the "big-endian hardware run" this section used to list
  as an open item), and (2) byte-compares `emit.c`'s output built once natively (LE) and once
  cross-compiled (BE) via `cmp` (the literal R4 wire-format proof). `crosscheck`/`fuzz_decode.py`
  and `stress.c` deliberately stay native-only: ctypes can't call into a cross-compiled foreign-
  architecture `.so`, and `stress.c`'s 150k-iteration loop gains nothing from emulation once
  `test_pfc` already covers BE correctness.

**Status: pushed and executed for real (first run: #15/#16 on the `ugreen-nas` Gitea Actions
runner).** The overnight-authored workflow was reasoned-correct but unexecuted; the first actual
run surfaced real, useful results — both environment bugs in the workflow itself and a genuine
memory-safety bug in the code:

- **`bigendian` — ✅ PASSED for real.** The full 139-check `test_pfc` suite ran on emulated
  big-endian PowerPC via `qemu-ppc` and the `emit.c` LE-vs-BE byte-comparison matched. This is the
  actual R4 execution proof, not just the wire-format reasoning — genuinely closes that open item.
  Also confirms the runner has real outbound internet access (apt archive + GitHub for actions),
  resolving the biggest flagged unknown from the overnight authoring.
- **Two environment bugs found + fixed:** (1) the runner's `ubuntu-latest` label maps to
  `node:20-bookworm` (act's own default for an unconfigured self-hosted runner, not a real Ubuntu
  image) — it runs as root already and has no `sudo` binary, so every job died in ~10s on the
  first `sudo apt-get` line; fixed by dropping `sudo` (jobs run as root in this container). (2)
  `actions/setup-python@v5` can't find a matching Python 3.12 build for this runner's self-
  reported platform (a known friction point for `act`-based self-hosted runners against
  GitHub's hosted-runner-oriented version manifest); fixed by installing `python3`/`python3-numpy`
  directly via `apt-get` instead (Debian-based image, no PyPI/manifest lookup needed).
- **`misra` — ran successfully end-to-end** (`apt-get install cppcheck` → `make misra`) and
  correctly **failed the build on 179 real MISRA-C:2012 findings across 17 rules** — the gate
  itself works correctly. **Triage now done — see `flight/docs/misra-deviations.md` for the full
  rule-by-rule record.** Summary: 4 findings were genuine and trivially fixed (two missing `else`
  branches, one loop-variable reuse); 27 were confirmed cppcheck-MISRA-addon false positives
  (comma-separated declarator lists misidentified as the comma operator, and cross-file function
  usage the single-file scan can't see) — suppressed with a documented reason; the remaining 148
  are deliberate, verified-safe patterns that conflict with an Advisory rule (early-return
  defensive style, explicit widening casts, void* buffer handling, etc.) — each spot-checked
  against real source, not assumed, and suppressed via `flight/.cppcheck-suppressions` with a
  written justification per rule. `make misra` now applies that suppression list, so the gate
  stays meaningful (anything not on the reviewed list still fails the build) rather than silenced.
- **`libfuzzer` — found a real heap-buffer-overflow within ~2 minutes of fuzzing.** ASan:
  `SUMMARY: AddressSanitizer: heap-buffer-overflow flight/src/pfc_internal.h:134:22 in
  pfc_get_u32`, called from `pfc_spectral_decode` (`pfc_spectral.c:219`). Root cause: SPECTRAL's
  header is `PFC_SPEC_HDR` (24 bytes) but the top-level `pfc_decode()` dispatcher only validates
  `src_len >= PFC_HDR` (20 bytes) before routing to a codec — correct for the other four codecs,
  whose header *is* exactly `PFC_HDR`, but insufficient for SPECTRAL. A 20–23-byte crafted input
  (`PFC1`+ver+codec=5+garbage) passed that check and `pfc_spectral_decode` read `s[20..23]`
  unconditionally, one byte past the buffer. **Fixed:** added an explicit
  `if (len < PFC_SPEC_HDR) return PFC_E_CORRUPT;` at the top of `pfc_spectral_decode`, before any
  header byte is touched — matches how the other codecs rely on (and satisfy) the dispatcher's
  generic check, since only SPECTRAL's header size differs. Added a regression test in
  `test_pfc.c` (`test_spectral_corruption`) using the exact crashing input. **Notable:** the
  Python-based `fuzz_decode.py` ran 20,000 iterations in the earlier overnight session and found
  nothing — libFuzzer's coverage-guided mutation found this in under 2 minutes, a concrete
  demonstration of why the C/ASan/libFuzzer gate (not just the Python harness) is worth having.
- **`native` — ✅ FIXED and confirmed on the next run.** The `setup-python` fix (installing
  `python3`/`python3-numpy` via `apt-get` instead) worked: `make check` — strict `-Werror` build,
  ASan/UBSan, the R7 independent-decoder cross-check, decoder fuzz, and 15k-case stress — all
  passed in ~73s on the second run.
- **`libfuzzer` — found a SECOND real bug immediately after the SPECTRAL fix, in the very same
  120s run.** With the SPECTRAL crash gone, fuzzing continued (visible in the log: coverage climbed
  past 1,088,000 executions) and found a heap-buffer-overflow in `pfc_columnar_decode`
  (`pfc_columnar.c:150`, both ASan and a UBSan `index 65536 out of bounds for
  uint8_t[65536]` on the same line). Root cause: `pfc_columnar_encode` always derives
  `block_recs = PFC_BLOCK_BYTES / rw` so `rw * block_recs` can never exceed the fixed
  `w->xform[PFC_BLOCK_BYTES]` scratch buffer it's transposed through — but
  `pfc_columnar_decode` reads `block_recs` straight from the untrusted stream header with **no
  upper-bound check at all**. A crafted header (`rw=2, cnt=block_recs=40000` → `block_bytes =
  80000 > 65536`) writes past the buffer. Checked `pfc_seq_decode` for the same class of bug —
  it's safe, because it writes directly into the caller's already-capacity-checked `dst` and never
  uses the shared `xform` scratch buffer at all; `pfc_columnar_decode` is the only codec that
  transposes through `xform`, so it's the only one exposed. **Fixed:** added
  `if (block_bytes > PFC_BLOCK_BYTES) return PFC_E_CORRUPT;` right after computing `block_bytes`
  each iteration, before any block-record read is even attempted. Added a regression test
  (`test_columnar_oversized_block`) — a pure 20-byte crafted header, no payload needed, since the
  new check fires before the block-record path is reached.

**Confirmed on the following runs: all four jobs green except `misra`.** Two real bugs found and
fixed by `libfuzzer` in its very first runs — both header-size/header-value validation gaps, in
the newest codec (SPECTRAL) and the most structurally distinct existing one (COLUMNAR, the only
codec using the `xform` scratch buffer). `native` and `bigendian` confirmed fully green after a
test-setup bug in my own regression test was found and fixed (wrong `cap` value — the underlying
fix was already correct). `misra` remains red **by design** until the findings below are
addressed in the codebase — see the MISRA triage section further down.

### CBMC proof: pfc_block_read, and a THIRD real bug found writing it

Authoring the `cbmc` job's harness (`proofs/cbmc/harness_block_read.c`) surfaced a real bug before
the proof ever ran in CI — reading `pfc_block_read`'s bounds check with "does this hold for any
`size_t` width" in mind, not just "does this pass on this 64-bit host", made it visible by hand:

```c
if ((p + PFC_BLKHDR + n) > len) { return PFC_E_CORRUPT; }
```

`n` is the raw 4-byte `payload_len` field, read straight off the wire via `pfc_get_u32` —
attacker/corruption-controlled, up to `UINT32_MAX`. On the 64-bit host every other CI job runs
on, `p + PFC_BLKHDR + n` never overflows `size_t`, so this check has always behaved correctly in
every test this project has ever run — `native`, `libfuzzer` (170k Python iterations, then real
coverage-guided libFuzzer), `bigendian` (`qemu-ppc` is still a 64-bit host process emulating a
32-bit *instruction set*, not a 32-bit `size_t` C runtime). But the actual flight targets
(RAD750, LEON/SPARC, RISC-V-32) are 32-bit machines where `size_t` really is `uint32_t`. There, a
crafted `payload_len` near `UINT32_MAX` makes `p + PFC_BLKHDR + n` wrap around 2^32 to a small
value, satisfies `<= len`, and the function proceeds to treat a multi-gigabyte `n` as a valid
in-bounds payload length — `pfc_crc32(*payload, n)` and whatever codec consumes `*payload`/`*plen`
next would read far past the actual buffer. Every codec's decoder funnels through this one
function (`pfc_spectral.c`, `pfc_seq.c`, `pfc_image.c`, `pfc_columnar.c` all call it directly), so
this was a single point of exposure for the whole decoder on 32-bit flight hardware specifically —
and specifically the class of bug no gate in this pipeline could have found: it's invisible under
every sanitizer, every fuzz run, and every functional test executed so far, because they all run
on 64-bit hosts. This is the textbook case for formal methods over testing: the property depends
on a machine model (32-bit `size_t`) that nothing else in this CI pipeline exercises.

**Fixed** in `pfc_block_read` (and, defensively, the symmetric `pfc_block_write`, even though its
inputs are locally-trusted encoder-side values, not downlink data): rewrote the bounds check to
subtract already-validated quantities instead of summing untrusted-scale ones —

```c
if ((p > len) || ((len - p) < PFC_BLKHDR)) { return PFC_E_CORRUPT; }
rem = (len - p) - PFC_BLKHDR;
if ((size_t)n > rem) { return PFC_E_CORRUPT; }
```

— which cannot overflow on any `size_t` width: `rem` is a subtraction of two quantities already
proven `len - p >= PFC_BLKHDR`, and the final `*pos = p + PFC_BLKHDR + n` is only reached once `n
<= rem` is proven, which bounds that sum by `len` itself. Added a host-level regression test
(`test_block_read_bounds` in `test_pfc.c`) locking in the exact boundary (`n == rem` accepted, `n
== rem + 1` rejected) — though that test can only prove the boundary is correct under the host's
own 64-bit `size_t`; it cannot exercise the wraparound itself. **That's what the CBMC proof is
for**: `harness_block_read.c` run with `--32` (`make cbmc` / the `cbmc` CI job) models a real
32-bit `size_t` and asserts the function's full documented contract — on `PFC_OK`, `payload`/`plen`
lie entirely within `src[0..len)` and `pos` advances by exactly the block size; on rejection, `pos`
never moves backward and never advances past `len` (not "unchanged" — see below, an earlier
version of this harness asserted that and CBMC correctly caught it as false). Proved on the fixed
code; the old (pre-fix) code would fail this proof under `--32` with `--unsigned-overflow-check`,
which is exactly the point.

**Getting the `cbmc` job green took four more real fixes, none of them the proof itself being
wrong about the code:**

1. **Run #25 failed at tool install.** `apt-get install cbmc` doesn't work on this runner: Debian
   bookworm's default repos don't carry a `cbmc` package at all (`E: Unable to locate package
   cbmc`) — unlike `cppcheck`/`clang`, this one just isn't packaged for Debian stable. Fixed:
   install the upstream release `.deb` directly (`ubuntu-22.04-cbmc-6.10.0-Linux.deb` from
   `github.com/diffblue/cbmc/releases`), pinned to a specific version for reproducibility.
2. **Automated commit review flagged the install as unverified binary execution.** Fair — curling
   a binary and running `dpkg` on it with no integrity check. Fixed: verify its sha256 (the digest
   GitHub's release API reports for this exact asset, independently re-downloaded and re-hashed by
   hand to confirm) via `sha256sum -c` before `dpkg` ever touches the file.
3. **Run #27: install succeeded, but `cbmc` itself errored with "Usage error!" (exit 2).**
   Reproduced locally with Docker (same `node:20-bookworm` base image) instead of iterating via
   push-and-wait — much faster to debug. Root cause, found in the buried help-dump output: `Unknown
   option: -std=c99`. CBMC's own C frontend doesn't accept GCC's `-std=c99`; it wants `--c99`.
   Fixed in the Makefile.
4. **Locally, with that fixed: `--32` failed at preprocessing** — `fatal error:
   bits/libc-header-start.h: No such file or directory`. `--32` needs actual 32-bit libc headers to
   preprocess against, which the runner's Debian container doesn't have by default. Fixed: `apt-get
   install gcc-multilib` in both workflow copies (~25MB, one-time per job run).
5. **With the tool finally running correctly, the proof itself ran — and found two real issues,**
   both fixed with actual corrections, not suppressions:
   - The harness's own assertion was wrong: it claimed `pos` is unchanged on *any* rejected block,
     but `pfc_block_read` deliberately advances `pos` on a CRC-mismatch rejection ("corrupt block,
     but framing stays in sync" — so a caller can keep reading subsequent blocks after a corrupt
     one). CBMC correctly found a counterexample. Fixed the harness to assert the real contract:
     `pos` never moves backward and never exceeds `len`, matching the literal "reads never advance
     out of bounds" promise in `pfc_frame.c`'s header comment.
   - `pfc_crc32`'s branchless mask (`uint32_t mask = 0u - (crc & 1u);`) is well-defined C (unsigned
     wraparound is modular, not UB) but reads as an arithmetic overflow to
     `--unsigned-overflow-check`, which flags it regardless of intent. Rewrote as an explicit
     branch — behaviorally identical (confirmed via a full local `make check`: 145 unit + 7
     crosscheck + 20k fuzz + 15063 stress, 0 failures, same compressed-size numbers), zero
     functional change, no more CBMC noise.

**Confirmed on run #28: all 5 jobs green, `cbmc` reports `** 0 of 165 failed (1 iterations) /
VERIFICATION SUCCESSFUL`.** Verified locally end-to-end before pushing (same steps as the real CI
job, via Docker) to avoid another push-and-wait round-trip, then confirmed the real CI run matched
exactly. This closes out the CBMC next-step item for `pfc_block_read`.

### Extending past pfc_block_read: a FOURTH real bug, more severe than the first three

Reading the other three codecs' decoders for the same class of bug (chaining untrusted wire-header
fields into a size check) found one immediately, and it's worse than the `pfc_block_read` bug:
SPECTRAL's `cap < ((size_t)width * height * count * es)` multiplies four factors derived from
untrusted input — `width` (bounded to `PFC_MAX_COLS`, 13 bits), `height` and `count` (raw 32-bit
wire fields, unbounded), `es` (1 bit) — up to **78 bits total**. `pfc_block_read`'s bug only
manifested on a 32-bit `size_t` (the encoder-side flight target; per `pfc.h`'s own doc comment the
*decoder* is meant to run ground-side, typically 64-bit) — but 78 bits overflows a 64-bit `size_t`
too, so this one is reachable on the actual deployed ground decoder, not just a defensive concern
for hypothetical 32-bit reuse.

Concrete proof it's real: `width=2, height=count=2^31, es=2` (bd=16) makes the true product
**exactly `2^64`** — which a naive 64-bit `(size_t)width*height*count*es` computes as `0` (full
wraparound). The old check `cap < total` became `cap < 0`, which is always false for an unsigned
`cap`: **any** `cap`, including `0`, would have been accepted as "big enough" for a stream that
actually needs `2^64` bytes. Added as a regression test (`test_pfc.c`, the new case in
`test_spectral_corruption`) asserting `PFC_E_BOUND` for exactly this crafted 24-byte header.

Fixed with a small reusable helper, `pfc_size_mul` (`pfc_internal.h`) — the standard CERT C
INT30-C division-guarded-multiply idiom (`if (a != 0 && b > SIZE_MAX/a) overflow; else *out =
a*b;`), correct by construction and hand-verified in its doc comment. Applied to all four codecs'
decode-side size checks (SPECTRAL's the only one reachable at 64 bits; IMAGE/COLUMNAR/SEQ fixed
defensively for the same 32-bit-reuse reason `pfc_block_read` was, though their two/three-factor
products can't overflow 64 bits given their existing bounds).

**Attempted a CBMC proof of `pfc_size_mul` itself** (fully generic, unconstrained `size_t` domain,
at both `--32` and native 64-bit) — the SAT problem for unconstrained multiplication-overflow
checking didn't converge within a 10-minute budget on either width, and got killed both times.
Rather than force through an unreliable/slow CI gate for a well-established four-line idiom, left
it as a hand-verified proof-sketch in the source comment instead — a division-guarded multiply is
provably correct via basic integer-division properties (rounds down, so `a*(SIZE_MAX/a) <=
SIZE_MAX` always) without needing exhaustive bit-blasting. Confirmed via full local `make check`
(146 unit + 7 crosscheck + 20k fuzz + 15063 stress, 0 failures) that the fix is behaviorally
correct in the cases that matter, even without the generic CBMC proof.

**Attempted CBMC proofs of `pfc_bound` sufficiency + round-trip correctness** for the smallest
nontrivial SEQ case (count=1, elem=1, either signedness, ANY 1-byte input): same "didn't converge"
outcome as `pfc_size_mul` above, for a different reason. Two harnesses were written and run (32-bit
model, `--unwind 40`, same check flags as `pfc_block_read`'s proof): a combined
`pfc_decode(pfc_encode(x)) == x` round-trip proof, which didn't reach a verdict in 10 minutes; then
an encode-only `pfc_bound` sufficiency proof split out on the theory that it's a smaller problem —
it also didn't converge in a comparable window. Unlike `pfc_block_read` (pure bounds arithmetic,
the case this toolchain has proven tractable) or `pfc_size_mul` (unconstrained-domain arithmetic),
this pulls in the range coder's carry/renormalisation branching and the adaptive category model's
per-symbol frequency-table updates — real, data-dependent state, not just a wider input domain, and
CBMC's bounded model checking scales badly with that kind of branching even for a single symbol.
Both harness files were deleted rather than left in `proofs/cbmc/` unrun (same handling as
`pfc_size_mul`'s abandoned attempt — no dangling broken proof, just this writeup). The property
remains covered only by testing (15 063 stress cases + the independent-decoder cross-check
exercise round-trip correctness and bound sufficiency empirically already), not by formal proof.
Re-attempting would need a larger time budget, a hand-built model of the range coder's invariants
instead of symbolically executing it, or a different solver backend — none pursued here.

## Universal safety-claim audit

Audit of universal quantifiers (`always`, `never`, `every`, `exactly`, `all`, `none`) in safety
claims across `flight/README.md`, `flight/docs/requirements.md` and
`flight/docs/mission-safety.md`. Each claim is either tied to a named harness/proof or annotated
with its exception. No remaining universal safety claim is unbacked.

| Claim | Location | Evidence / exception |
|-------|----------|----------------------|
| **No dynamic allocation** — `malloc`/`free`/`recursion` are absent | README, R2 | `nm -uD build/libpfc.so` shows **zero alloc symbols**; `src/` contains no `stdlib.h`; `stackdepth` tool asserts no recursion/function pointers. |
| **Bounded memory** — footprint = `pfc_workmem_bytes()` | README, R3 | `sizeof(struct pfc_ctx)` is compile-time constant; printed size verified at test startup. |
| **No expansion** — output never exceeds `pfc_bound()` | README, R5 | `test_pfc.c` + `stress.c` assert `encoded <= pfc_bound` for all codecs; two real under-estimate bugs found+fixed by this harness. Exception: formal CBMC sufficiency proof attempted (smallest SEQ case) and **did not converge** — property remains test-covered, not formally proved. |
| **Lossless for every valid input** | R1 | 146 unit + 15 063 stress round-trips across all codecs, 0 failures; real CyCIF + real AVIRIS byte-exact. Exception: exhaustive small-input CBMC proof attempted and **did not converge** — covered by testing, not formal proof. |
| **Deterministic / portable** — integer-only, canonical LE wire format, BE⇄LE interoperability | R4 | No FP types in `src/`; independent Python explicit-LE decoder cross-check; `bigendian` CI job runs full suite under emulated BE PowerPC + `emit.c` LE-vs-BE byte compare. |
| **Error containment: corrupt/truncated frame reported, never reads OOB or crashes — on any `size_t` width** | R6 | `test_pfc.c` fault/truncation/corruption tests; `make asan`; `fuzz_decode.py` 20k iterations; `libfuzzer` CI job; **CBMC proofs** of `pfc_block_read` (0/165 failed) and `pfc_block_write` (0/152 failed) under 32-bit model. Exception: **SPECTRAL does not contain to one block** — inter-band prediction propagates a single CRC-rejected block forward through the cube (measured, mitigable via refresh bands; documented in §2.5.1). |
| **`pfc_block_read` never reads OOB** | `mission-safety.md` §2.3 | CBMC proof (`proofs/cbmc/harness_block_read.c`, `cbmc` CI job, `--32`) — `VERIFICATION SUCCESSFUL` (0/165 failed). |
| **`pfc_block_write` never writes OOB / leaves `pos` unchanged on rejection** | `mission-safety.md` §2.3 | CBMC proof (`proofs/cbmc/harness_block_write.c`, `cbmc` CI job, `--32`) — `VERIFICATION SUCCESSFUL` (0/152 failed). |
| **Encoder-side SEU is wholly silent** (CRC detects zero encoder-side upsets) | `mission-safety.md` §2.5 | Measured: `make seu`, 7 200 single-bit trials across all five codecs, **0 CRC detections**; 99.9% no effect, 0.1% silently wrong data with `PFC_OK`. `SEU_BURST=N` extends this to adjacent-bit burst upsets. |
| **IMAGE/SEQ/COLUMNAR/FLOAT contain damage to one block; SPECTRAL does not** | README, R6, §2.5/§2.5.1 | `make seu` and `make containment`: IMAGE/SEQ/COLUMNAR/FLOAT 0 multiblock silent corruptions; SPECTRAL propagates across bands. Refresh bands bound propagation at configurable interval. |
| **No recursion / no function pointers** (exact stack-depth bound) | R10 | Static analysis of disassembly; `stackdepth` CI job asserts acyclicity and call-graph completeness; 464 B worst case on flight target ABI. |

## Other open items

- **Residual −1.3% vs JPEG-LS** on photon-noisy imagery (−0.5% at band 64) — finer context modelling
  that only pays off with bigger bands (memory/containment tradeoff); a directional entropy context regressed.
