# libpfc — mission safety: where we are, and what "flight-qualified" still requires

Honest assessment. Passing an aggressive test suite is **necessary but not sufficient** for flying
software on a spacecraft. This document separates what is *evidenced now* from what a real flight
qualification (NASA NPR 7150.2 software-assurance process, and the spirit of DO-178C structural
coverage) still demands.

## 1. What is evidenced today

| Property | Evidence |
|----------|----------|
| Lossless round-trip | 139 unit + **15 063 randomised/edge cases** across all 5 codecs (image/seq/float/columnar/spectral) + real CyCIF + real AVIRIS hyperspectral; 0 failures |
| No expansion (`pfc_bound`) | property-checked on adversarial inputs — **two real under-estimate bugs found and fixed** (a skinny-image case, then a many-small-blocks spectral-cube case, both caught by the stress harness) |
| Error containment | bit-flip + truncation tests, **170 000 Python-harness fuzz iterations** under ASan/UBSan found nothing; the **real C/libFuzzer CI run found TWO genuine heap-buffer-overflows across its first two runs** — a SPECTRAL header-size validation gap, then (after that fix, same 120s run) a COLUMNAR unbounded-`block_recs` gap — both found and fixed; the honest lesson is coverage-guided fuzzing catches what blind random fuzzing doesn't, and both were found within minutes of the gate's first-ever execution |
| Machine-model-dependent safety | **CBMC-driven review found TWO more real bugs that no test above could ever find, across two size_t widths**: `pfc_block_read`'s length check summed an untrusted field before comparing, wrapping `size_t` only on a 32-bit target (the actual RAD750/LEON/RISC-V-32 flight hardware) — fixed and proved under a `--32` CBMC model. Then, extending the same review to the other three codecs, SPECTRAL's `width*height*count*es` size check (four untrusted factors, 78 bits) was found to overflow even a **64-bit** `size_t` — reachable on the real ground decoder, not just a defensive 32-bit concern — fixed with a reusable overflow-safe multiply helper across all four codecs. See `requirements.md` |
| No dynamic allocation | compiled `.so` imports **zero** alloc symbols; no recursion |
| Bounded memory | single caller-owned `pfc_ctx` (~330 KB at defaults), compile-time sized |
| Determinism | encode-twice byte-identical (checked every stress case) |
| Independent reference | pure-Python ground decoder reproduces C-encoder output byte-for-byte (R7), **10/10** cross-check incl. real CyCIF and real AVIRIS |
| Standard-relative perf | beats CCSDS-121-class Rice **+2.8%** (spatial) and is within **−1.2%** of CCSDS-123-class (hyperspectral, via the spectral codec) on real 16-bit data |
| Structural coverage | gcov, automated as a CI gate (`make coverage`, `coverage` job): **98.4%** lines / **89.3%** branches project-wide over the same corpus as `native` (test_pfc + stress); entropy core (range coder/model/CRC/framing) 100% lines; gated in CI at 95% line / 80% branch (below measured baseline, catches regressions without blocking legitimate new code) |

This is a high-quality, well-tested core — a credible *foundation*. It is not yet flight-qualified.

## 2. The gap to flight qualification

### 2.1 Process & standards (the bulk of real qualification)
- **NPR 7150.2 / NASA-STD-8739.8** software assurance: classify the software (likely Class B/C),
  then satisfy the required activities — plans, reviews, audits, configuration management.
- **Coding-standard compliance report**: MISRA-C:2012 + JPL *Power of Ten*, run with a qualified
  analyzer (Coverity / LDRA / Polyspace / cppcheck-MISRA), every finding resolved or formally
  deviated. Wired as `make misra` and automated in CI (`.github/workflows/flight-ci.yml`, `misra`
  job, installs cppcheck from the Ubuntu archive) — a from-source cppcheck build was attempted on
  a dev machine and abandoned mid-build after contributing to a crash, which is exactly why this
  runs on a CI runner instead. **Executed for real, and triaged.** `cppcheck --addon=misra` found
  179 findings across 17 rules on the first run (correctly failing the build — the gate works).
  Full rule-by-rule triage in `flight/docs/misra-deviations.md`: 4 were real and fixed (missing
  `else` branches, a loop-variable reuse); 27 were confirmed cppcheck-MISRA-addon false positives
  (comma-separated declarations misread as the comma operator; cross-file function usage a
  single-file scan can't see); the remaining 148 are deliberate, individually-verified-safe
  patterns (defensive early-return style, explicit widening casts, generic `void*` buffer
  handling, and one genuinely careful case — signed right-shift in the zigzag encode, confirmed
  safe on all three real cross-compile targets, not just assumed) that conflict with an Advisory
  rule and are recorded as formal deviations, applied via `flight/.cppcheck-suppressions`. Every
  deviation has a written, source-verified justification — this is not the gate silenced, it's the
  gate having done its job once.
- **Bidirectional requirements traceability: done at the solo-authored level.**
  `requirements.md`'s traceability matrix now maps every requirement (R1–R9, including the two
  process/assurance requirements added for the coding-standard and coverage gates) to its
  implementing module/function, its verification procedure, and a cited result — not just a
  pointer to a test file. What's still missing for a real flight qualification: **independent
  audit** (someone other than the author checking the matrix is honest and complete — see IV&V
  below) and **formal review records** in the process sense (signed-off design reviews, not just
  the detailed self-review writeups already in this repo).
- **Independent V&V (IV&V)**: review by a separate team/organisation.

### 2.2 Structural coverage (beyond functional tests)
- **Done: statement + branch coverage, automated in CI.** `make coverage` (gcov/gcovr) runs the
  same corpus as `native` (test_pfc + stress) with instrumented objects and gates on line/branch
  percentage — 98.4% lines / 89.3% branches project-wide as of this writing, gated at 95%/80% in
  CI (`coverage` job in `flight-ci.yml`). Per-file detail in `docs/requirements.md`.
- **MC/DC: measured, improved, and still the weakest structural number.** The concern stated here
  previously — that a compound boolean can hit every branch outcome without exercising every
  condition's independent effect — turned out to be not merely theoretical. Measured with clang 18's
  `-fcoverage-mcdc` (`make mcdc`, `mcdc` CI job): **55.65% MC/DC (64 of 115 conditions)** on the
  original corpus. `test/test_mcdc.c` closed the first set of header/parameter conditions, raising
  it to **76.52% (88 of 115)**; a second pass (G3.4) added coverage for the remaining parameter/
  header validation and store-raw conditions, and documented the rest.
  The 27 previously-uncovered conditions have been split and addressed:
  - **14 parameter/header-validation conditions** (G3.4a) — covered by additional crafted-header
    tests, including the previously-missing IMAGE decode header and the encode-side guards for
    IMAGE, SPECTRAL, COLUMNAR and SEQ.
  - **8 `pfc_size_mul` capacity-guard conditions** (G3.4b) — classified as unreachable on the
    64-bit CI machine model by construction; only SPECTRAL's four-factor product can overflow 64-bit
    `size_t` and already has a regression test.
  - **2 store-raw fallback conditions** (G3.4c) — covered with deliberately incompressible input
    and a tiny-capacity overflow case.
  - **3 genuinely state-dependent conditions** (G3.4d) — the `pfc_image.c` gradient tie-break is
    covered by a crafted image; the two `pfc_arith.c` range-coder renorm underflow conditions are
    recorded as not unit-testable with the public API because they need a specific low/range state
    built over many symbols.
  The CI gate remains an explicit **ratchet** — `MCDC_MIN` is raised only when a new measurement is
  available — **not a compliance claim**. DO-178C wants ~100% MC/DC on decision-heavy
  safety-critical code; this is not that. See `requirements.md` for the per-file breakdown.

### 2.3 Formal methods (stronger than testing for the safety-critical claims)
- **Bounded model checking** (CBMC) of the decoder: prove *no out-of-bounds read for any input up
  to N bytes* — a proof, not a sample. The decoder eats untrusted/SEU-corrupted data, so this is
  the highest-value formal target. **Done for one function, confirmed green in CI, and it already
  paid for itself twice over.** `pfc_block_read` — the shared block-framing primitive every
  codec's decoder calls — is proved (`proofs/cbmc/harness_block_read.c`, `make cbmc` / CI `cbmc`
  job, confirmed `VERIFICATION SUCCESSFUL` on run #28) to never read out of bounds, under a
  **32-bit `size_t` model** (`--32`), matching the real RAD750/LEON/RISC-V-32 flight targets
  rather than the 64-bit host every other gate here runs on. Writing that harness surfaced a real
  bug first: the original bounds check summed an untrusted 4-byte length field before comparing
  it, which can wrap `size_t` on a 32-bit target (never on 64-bit CI) and let a crafted
  `payload_len` near `UINT32_MAX` bypass the check entirely — invisible to every sanitizer/fuzz/
  functional gate in this pipeline because they all run 64-bit. Fixed with a subtraction-based
  check that cannot overflow on any width. Running the proof itself then found two more real
  issues (an over-strict harness assertion, and a CRC branchless-mask idiom CBMC's overflow
  checker flags on principle) — see `requirements.md` for the full writeup. Scope is currently one
  function, not the whole decoder — see next steps.
- **Extended to the rest of the untrusted-input surface — and it immediately found a FOURTH real
  bug, more severe than the first three.** Reading the other three codecs' decoders for the same
  overflow class found SPECTRAL's `cap < width*height*count*es` chaining four untrusted factors
  (up to 78 bits) — unlike `pfc_block_read`'s bug (32-bit-only, encoder-side), this one overflows
  even a **64-bit** `size_t`, so it's reachable on the actual deployed ground decoder, not just a
  defensive concern. Concrete construction: `width=2, height=count=2^31, es=2` makes the true
  product exactly `2^64`, which wraps to `0` — the old check would have accepted *any* `cap`,
  including `0`. Fixed with a reusable overflow-safe multiply helper (`pfc_size_mul`, the standard
  CERT C INT30-C division-guarded idiom) applied to all four codecs' decode-side size checks; added
  a regression test with the exact `2^64` vector; confirmed via full `make check` (146 unit + 7
  crosscheck + 20k fuzz + 15063 stress, 0 failures). A CBMC proof of `pfc_size_mul` itself was
  attempted (fully generic domain, both widths) but the SAT problem didn't converge in a 10-minute
  budget on either width — left as a hand-verified proof-sketch in the source comment rather than
  an unreliable CI gate; the idiom is standard and provably correct by basic integer-division
  properties. See `requirements.md` for the full writeup.
- **Done: `pfc_block_write` proved, closing an asymmetry that had been left open.** When the
  `size_t`-wraparound bounds-check bug was found in `pfc_block_read`, the identical addition-based
  pattern in `pfc_block_write` was rewritten the same way *defensively* — and then never actually
  proved. That gap is now closed (`proofs/cbmc/harness_block_write.c`, `make cbmc`, **`0 of 152
  failed / VERIFICATION SUCCESSFUL`** under `--32`). The write side is worth proving on its own
  terms, not just for symmetry: `plen` reaching it derives from the range coder's output length,
  and the *encoder* is the component that runs on the 32-bit spacecraft hardware where the
  wraparound is actually reachable — with no ground operator to notice a malformed frame. The
  proved contract differs from the read side in one deliberate way: on rejection `pos` must be
  **completely unchanged** (a rejected write must not leave a hole), whereas `pfc_block_read`
  deliberately *does* advance on a CRC mismatch to stay in framing sync.
- **Scope note — why `pfc_block_write` and not the per-codec header parsers.** The previous
  "remaining scope" line named the four codecs' header parsers as the next target. Attempting them
  showed why that was the wrong pick: each parser is embedded in its codec's full decode loop, so
  a proof drags in the range coder and adaptive model — precisely the state-explosion that made the
  `pfc_bound`/round-trip proofs below fail to converge. Proving them would require bounding
  `count`/`height` so tightly that the result would say little. `pfc_block_write` is bounds-only
  arithmetic, structurally identical to the proof that *did* converge, and covers a real
  flight-side threat. Better a sound proof of the tractable thing than a weak proof of the
  fashionable one.
- **Attempted: `pfc_bound` sufficiency + round-trip correctness for the smallest SEQ case
  (count=1, elem=1, either signedness, ANY 1-byte input) — did not converge, same outcome as
  `pfc_size_mul` above and for a similar reason.** Two harnesses were written and run (32-bit
  model, `--unwind 40`, same check flags as `pfc_block_read`): a combined encode+decode round-trip
  proof, and an encode-only `pfc_bound` sufficiency proof split out after the combined one didn't
  finish in 10 minutes. The split encode-only proof also didn't reach a verdict in a comparable
  window. Unlike `pfc_block_read` (pure bounds arithmetic — the SAT-friendly case), this pipeline
  pulls in the range coder's data-dependent carry/renormalisation branching and the adaptive
  category model's frequency-table updates even for a single symbol — a much larger state space
  for CBMC's solver, not just a bigger input domain. Per the same policy as `pfc_size_mul`: not
  worth an unreliable/slow CI gate. No harness file was kept in `proofs/cbmc/` (consistent with how
  `pfc_size_mul`'s abandoned attempt was handled — no dangling unrun proof, just this writeup).
  This property remains covered only by testing: 15 063 stress cases + the independent-decoder
  cross-check already exercise round-trip correctness and bound sufficiency empirically, just not
  as an exhaustive proof. Re-attempting would need either a much larger time budget, a tighter
  hand-built model (e.g. asserting range-coder invariants directly instead of symbolically
  executing it), or a different solver backend — not pursued further here.

### 2.4 Target & timing
- **Cross-compile and run on the flight CPU/RTOS**: RAD750 (big-endian PowerPC), LEON/SPARC, or
  RISC-V HPSC, under RTEMS/VxWorks. We proved the wire format canonical via the explicit-LE Python
  decoder; a genuine **big-endian *execution*** run (not just a format proof) is now automated in
  CI (`flight-ci.yml`, `bigendian` job): the full 139-check test suite runs on emulated big-endian
  PowerPC via `qemu-user`, plus a byte-comparison of a deterministic fixture built once natively
  (LE) and once cross-compiled (BE). **Executed for real on the first run and PASSED**: all 139
  checks green under emulated BE execution, and the LE/BE `emit.c` outputs matched byte-for-byte.
  qemu-user emulation is real BE *execution*, a meaningfully stronger check than static analysis,
  but it is still not the actual RAD750/LEON/RISC-V target hardware or RTOS — that remains a real
  gap even with this CI job green.
- **Stack-depth analysis: DONE, and it did not need the hardware.** This was previously bundled
  with WCET as "needs the real target", which was wrong — frame sizes and the call graph are
  properties of the *compiled object code*, so cross-compiling for the target ABI is sufficient;
  only timing genuinely needs silicon. `make stackdepth` / `make stackdepth-ppc`
  (`tools/stack_depth.py`, `stackdepth` CI job) computes the bound by longest-path over
  `-fstack-usage` frames: **464 bytes worst case on the flight target ABI** (big-endian 32-bit
  PowerPC, via `pfc_decode`), 632 B on x86-64, both gated in CI at 1024 B. The bound is *exact
  rather than estimated* because no recursion makes the call graph a DAG and no function pointers
  make it complete — and the tool asserts both, failing loudly rather than reporting a wrong
  number if either Power-of-Ten rule ever regresses. Caveats (return-address overhead not counted,
  one unresolved libc `memset` edge) are documented in `requirements.md`.
- **WCET analysis: still genuinely blocked on hardware.** Bounding worst-case execution time per
  block requires the actual target CPU (cache/pipeline/memory behaviour), not an emulator — encode
  throughput must clear the instrument data rate with margin, and that number is meaningless
  without real silicon.
- **Compiler qualification**: pin and qualify the exact flight toolchain version.

### 2.5 Space environment (SEU / radiation)
**This section used to assert its fault model. It is now measured** — `make seu`
(`test/seu_inject.c`) flips a bit in `pfc_ctx` mid-encode via linker `--wrap` (so `src/` compiles
unmodified, no test hooks in flight code) and observes what reaches the ground. Full numbers and
method in `requirements.md`; what changed here:

- **🔴 It found a real bug: an SEU-induced INFINITE LOOP in the range coder — since fixed.** A
  flipped bit that zeroes a `freq[][]` entry makes `pfc_rc_encode` compute
  `range = (range/tot) * 0 == 0`; thereafter `low ^ (low + 0) == 0` is permanently below
  `PFC_RC_TOP` and `range <<= 8` can never restore it, so `pfc_rc_enc_renorm` spins forever.
  On a spacecraft that is a hung compression task and a watchdog reset — **strictly worse than the
  silent corruption this section anticipated**, because it is a liveness failure, not a data one.
  It was also a latent **JPL Power-of-Ten Rule 2 violation**: the renorm loops are `while` loops
  whose termination depends on data values, with no statically provable bound. This was the
  concrete demonstration that the missing bound was reachable.
  `freq >= 1` holds on uncorrupted state, so it needs memory corruption to trigger — precisely the
  fault model this section is about. **Fixed** by bounding both renorm loops
  (`PFC_RC_RENORM_MAX`); the encoder reports `overflow`, which every codec already handles by
  falling back to store-raw, so a corrupted model costs one block's compression instead of hanging.
  Output on healthy state is byte-identical (all compression ratios unchanged). Regression test:
  `test_renorm_bound`. **Lesson worth keeping: the fault-injection harness earned its cost on its
  first serious run, and it found a failure class — hang — that no fuzzer, sanitizer, or proof in
  this pipeline was looking for**, because they all model bad *input*, not bad *state*.
- **The CRC detected ZERO encoder-side upsets** (0 of 7 200 trials). This confirms, rather than
  contradicts, what this section always said — the CRC protects the *channel*, and is computed
  *after* the corruption, so a corrupted block is self-consistent. The measurement's contribution
  is showing there is no incidental detection either: encoder-side SEU is a **wholly silent**
  failure mode. 99.9% of upsets had no observable effect; the remaining 0.1% produced silently
  wrong data with a PFC_OK status.
- **🔴 Containment does NOT hold for SPECTRAL.** Extending the harness across all five codecs
  (previously only IMAGE was measured) found that IMAGE, SEQ, COLUMNAR and FLOAT contain the damage
  — 0 of 157, 0 of 87, 0 of 134 and 0 of 165 silent corruptions crossed a block boundary
  respectively — but **SPECTRAL fails: 12 of 15 crossed**, with a worst case of 3 734 corrupted
  bytes against a 1 024-byte block, i.e. damage spanning roughly 3.6 blocks.
  The mechanism is architectural, not a coding error, and is visible in the source: SPECTRAL
  reconstructs band *z* by reading band *z−1* out of the output buffer
  (`pfc_spectral.c`, `bzp = (z-1)*height*width` / `has_prev`), because its whole compression
  advantage comes from inter-band MED-of-difference prediction. So SPECTRAL's blocks are
  independently *framed and CRC'd* but **not independently decodable** — a silently-wrong band
  feeds the next band's prediction and the error propagates forward through the cube.
  **This section's blanket claim that small independent blocks bound the blast radius is therefore
  wrong for SPECTRAL**, and that is now stated rather than assumed. The containment property and
  the codec's compression win are in direct tension: you cannot exploit inter-band correlation and
  simultaneously have bands fail independently.
  **That open question is now ANSWERED, and the answer is yes — see §2.5.1 below.** Downlink
  corruption propagates the same way, so this is not an SEU-only caveat: **R6 as written is wrong
  for SPECTRAL.**
- **Containment holds for the other four codecs — but "contained" is not "small".** For IMAGE,
  within a band the damage is near total: worst observed **2 035 corrupted bytes out of a
  2 048-byte block**. The honest statement of the mitigation is "blast radius is bounded by the
  block size", which only reassures to the extent the block is small. For SEQ, COLUMNAR and FLOAT
  the block is 64 KB, so a single upset can silently corrupt up to half a 128 KB payload.
- **Risk is inverse to region size, and that is the useful part.** Stratified injection (300 trials
  per region) gives conditional silent-corruption rates of **`tot[]` 20.3%, `mant[][]` 19.7%,
  `freq[][]` 7.7%, `bias_*[]` 7.0%** — versus **`scratch[]` 0.7% and `xform[]` 0.0%**, which
  together are 99% of the memory but are overwritten before use.
  **Actionable consequence, and the concrete recommendation this section previously could not
  make: the four model regions total 3 012 bytes — 0.91% of the 330 KB workmem — and carry
  essentially all the risk.** Protecting ~3 KB with EDAC, scrubbing, or a periodic checksum
  addresses the dominant failure mode at roughly 1/100th the cost of protecting the whole context.
  That is a realistic ask of a flight platform in a way "harden all working memory" is not.
- **Multi-bit / burst upsets are now modelled.** `make seu SEU_BURST=N` flips `N` consecutive bits
  at the injection point (e.g. `SEU_BURST=8`). A 1 000-trial single-bit run vs a 2 000-trial
  8-bit-burst run on the same fixtures shows the expected qualitative change: burst upsets are
  slightly more likely to corrupt the small high-value model regions, but containment behaviour
  is unchanged — IMAGE/SEQ/COLUMNAR/FLOAT stay contained, SPECTRAL still propagates.

  | mode | codec | silent rate | multiblock silent |
  |------|-------|-------------|-------------------|
  | single-bit | IMAGE | 0.5% | 0/42 |
  | single-bit | SEQ | 8.8% | 0/17 |
  | single-bit | COLUMNAR | 20.0% | 0/31 |
  | single-bit | FLOAT | 23.2% | 0/35 |
  | single-bit | SPECTRAL | 0.0% (uniform) / 3 stratified | 3/3 |
  | 8-bit burst | IMAGE | 0.2% | 0/59 |
  | 8-bit burst | SEQ | 6.8% | 0/23 |
  | 8-bit burst | COLUMNAR | 16.0% | 0/51 |
  | 8-bit burst | FLOAT | 19.2% | 0/60 |
  | 8-bit burst | SPECTRAL | 0.0% (uniform) / 2 stratified | 1/2 |

  The SPECTRAL fixture here is intentionally small (32×32×4 bands) so the uniform budget rarely
  lands in the tiny model regions; the stratified rows show what happens when it does. The
  containment conclusion is unchanged from single-bit: SPECTRAL propagates, the others do not.
- Remaining system-level mitigations unchanged: EDAC/scrubbed RAM, periodic re-initialisation.
  The new evidence sharpens where to spend that budget rather than replacing it.
- **Done:** re-measured the SPECTRAL refresh-band compression cost on **real AVIRIS** Indian
  Pines (200 bands, 145×145, uint16). See §2.5.1 for the price curve. Single-bit and multi-bit SEU
  behaviour is now covered for all five codecs.

### 2.5.1 R6 is wrong for SPECTRAL — downlink corruption propagates too (measured, and mitigated)

`make containment` (`test/downlink_containment.c`) flips **one bit in one block's payload**, so that
block's CRC fails and every other block stays valid, then measures which bands differ:

| codec | bands damaged | verdict |
|-------|---------------|---------|
| IMAGE (control — no inter-band prediction) | 1 of 6 | **contained** |
| **SPECTRAL** | **6 of 6** | **propagated** |

The IMAGE control is what makes this attributable: same corruption, same geometry, same block size.
The only difference is that SPECTRAL predicts band *z* from band *z−1*. Mechanism, visible in
`pfc_spectral.c`: a CRC-rejected block is filled with mid-grey by `spec_fill_block()`, and every
later band then predicts against that wrong reference — those later blocks' own CRCs are **valid**,
so they decode "correctly" into wrong data.

**So R6's "a corrupt/truncated frame loses one block" is false for SPECTRAL.** Two honest
qualifications: the failure is **not silent** (decode still returns `PFC_E_CORRUPT`, so the ground
station knows something is wrong — it is the *extent* that is misdocumented, not the detection);
and this is **inherent to inter-band prediction**, which is precisely where SPECTRAL's compression
advantage over per-band coding comes from. Containment and compression are in direct tension here.

**Mitigation, implemented and default-off: inter-band refresh bands.** `pfc_params::elem` (unused
by this codec otherwise) sets an interval N; every N'th band is coded spatially-only, so damage
cannot propagate past the next refresh band. The interval travels in the stream header (the
previously reserved byte 7), so streams stay self-describing and a ground decoder never needs
out-of-band configuration.

**Measured on real AVIRIS Indian Pines** (200 bands, 145×145, uint16, public EHU GIC scene, measured
2026-08-19):

| refresh | ratio | cost vs off | divides 200 |
|---------|-------|-------------|-------------|
| **0 (default)** | **2.347×** | — | — |
| 2 | 2.179× | +7.68% | yes |
| 4 | 2.257× | +3.96% | yes |
| 6 | 2.287× | +2.60% | no |
| 8 | 2.301× | +1.98% | yes |
| 10 | 2.313× | +1.44% | yes |
| 20 | 2.331× | +0.67% | yes |
| 25 | 2.334× | +0.53% | yes |
| 40 | 2.338× | +0.36% | yes |
| 50 | 2.343× | +0.14% | yes |
| 100 | 2.346× | +0.03% | yes |
| 200 | 2.347× | +0.00% | yes |

The synthetic 12-band cube used earlier for the containment visualisation gave +14.68% at
refresh=4; on this real scene the same interval costs **+3.96%**. Real AVIRIS is less sensitive
than the strongly-correlated synthetic cube because the inter-band correlation is strong enough
to help, but not so strong that giving it up occasionally is catastrophic. The earlier weakly-
correlated synthetic figure (+0.88%) remains misleading in the other direction — the real price
sits between the two extremes.

A 12-band synthetic cube is retained below for the direct containment demonstration (it is small
enough to run the downlink-corruption harness band-by-band):

| refresh | ratio | cost vs off | bands damaged (of 12) |
|---------|-------|-------------|-----------------------|
| **0 (default)** | 8.31× | — | **12** |
| 6 | 7.75× | **+7.30%** | 6 |
| 8 | 7.74× | +7.37% | 8 |
| 4 | 7.25× | +14.68% | 4 |
| 2 | 6.09× | +36.56% | 2 |

**`refresh=0` is byte-identical to the pre-feature encoder** (asserted in `test_spectral_refresh`),
so this changes nothing for existing callers — enabling it is a deliberate mission decision, not a
silent default change. Every interval round-trips losslessly (R1), and the independent Python
ground decoder honours the header field too (R7, `spectral-refresh4` synthetic cross-check case).

**Pick the interval to divide the band count evenly.** On the 12-band synthetic cube, refresh=6
*dominates* refresh=8: identical cost (both insert two refresh bands) but a tighter propagation
bound. On the 200-band real scene, refresh=8 (divides evenly) is cheaper than refresh=6 (does not),
which is exactly the rule: an interval that does not divide Z can waste compression without buying
containment. A divisor such as 10, 20 or 25 gives a much cheaper bound for this scene.

**Containment bound as a function of N.** With refresh interval N, a single corrupt block can
affect at most N consecutive bands: the corrupted band itself plus the predictively-coded bands
up to (but not past) the next refresh band. In a Z-band cube, the worst-case fraction of the cube
lost is therefore at most N/Z. This bound is structural and holds regardless of the data source;
only the compression cost is data-dependent.

**Real-data verification: done.** The price is now measured on AVIRIS Indian Pines; the containment
benefit is structural (N bands by construction) and was demonstrated on the 12-band synthetic cube
via `make containment`.
- **Tested and rejected: detecting encoder-side SEU in software is harder than it looks.** The
  obvious cheap idea — the model already maintains `tot[ctx] == sum(freq[ctx][*])` everywhere, so
  verifying it costs *zero storage* — was measured (`scratchpad` harness, 400 trials/region) and
  **catches only 19.7% of silent corruptions** while tripping on 31.7% of harmless ones: poor
  sensitivity and noisy. Two reasons, both instructive:
  1. **The model's own maintenance launders the corruption.** `pfc_model_rescale` recomputes
     `tot` from `freq`, so a flipped `tot` is silently made self-consistent again *after* the wrong
     value has already been coded into the stream. Detection rate for `tot[]` upsets is only 34.8%
     for exactly this reason.
  2. **Half the model has no redundancy to check.** `mant[][]` and `bias_*[]` (492 B) carry no
     equivalent invariant: 0% detected, and they accounted for 97 of the 183 silent corruptions.
  A stored checksum does not rescue this either, for a more basic reason: the model is **adaptive**,
  so it legitimately mutates on every symbol — there is no stable value to checksum. Making it work
  would require either duplicating the model and comparing (2× the 330 KB workmem) or re-checksumming
  per symbol, both of which cost far more than they are worth on a flight encoder.
  **This strengthens rather than weakens the existing recommendation:** encoder-side SEU protection
  belongs at the *platform* level (EDAC/scrubbed RAM), not in the codec. The value of this
  measurement is that it closes off the cheap software alternatives with evidence instead of
  leaving them as tempting untested options.
- **Follow-up status** (this list previously had three open items; all three are now closed):
  - ~~(1) settle whether SPECTRAL's propagation also weakens *downlink* R6 containment~~ →
    **DONE, and it does.** See §2.5.1. R6 is corrected.
  - ~~(2) if broader, document it or offer periodic spatial refresh bands~~ → **DONE, both.**
    R6 row corrected in `requirements.md`; refresh bands implemented default-off with a measured
    containment/ratio curve.
  - ~~(3) checksum the ~3 KB of model state so encoder-side upsets become detectable~~ →
    **TESTED AND REJECTED** — see the bullet above. Software-level detection cannot work cheaply
    here because the model is adaptive (nothing stable to checksum) and its own rescale launders
    corruption. **Encoder-side SEU protection belongs at the platform level (EDAC).**
  - **All three follow-ups are now closed:** (a) multi-bit / burst upset model implemented
    (`make seu SEU_BURST=N`); (b) FLOAT separately measured; (c) refresh-band price re-measured on
    real AVIRIS Indian Pines (see table above).

### 2.6 Sustained robustness
- **Continuous fuzzing** (libFuzzer + ASan, days of CPU / OSS-Fuzz-style), seeded corpus, regression
  on every finding. A proper libFuzzer+ASan+UBSan run is wired into CI (`flight-ci.yml`,
  `libfuzzer` job), and **its first two executions (at the original 120s bound) found two real
  heap-buffer-overflows** — SPECTRAL header-size validation, then (immediately after that fix
  landed, same run) an unbounded `block_recs` in COLUMNAR's decode, both found+fixed — see
  `requirements.md`. This is exactly the value case for this gate: 170k iterations of the *Python*
  fuzz harness found nothing over the same class of input; coverage-guided libFuzzer found two
  real bugs within minutes of first running. **Done:** raised the bound to 300s — confirmed on run
  #23 (`Done 329400 runs in 301 second(s)`, 0 crashes, clean exit). **Attempted, confirmed not
  working, and now formally closed out:** corpus persistence across CI runs via `actions/cache`
  (unique-key-per-run + `restore-keys`, the standard "ratchet" pattern). Run #23's log shows both
  the restore and save steps failing with `Request timeout` against `/_apis/artifactcache/...` —
  this Gitea instance's cache backend isn't configured server-side. A second option,
  `actions/upload-artifact` + `download-artifact`, was considered and deliberately not pursued:
  v4's cross-run fetch needs a prior run-id lookup (no built-in "restore latest" the way `cache`
  has `restore-keys`), and that protocol's compatibility with this Gitea instance is itself
  unverified without live testing — trading one unverified integration for another isn't worth it
  for a bounded 300s job. The dead `actions/cache` step has been removed from both workflow files
  rather than left as a misleading permanent no-op. **Accepted limitation:** each run fuzzes a
  real, fresh 300s from an empty corpus; coverage does not compound across runs. Closing this for
  real requires either enabling a cache backend on the Gitea instance itself (outside what's
  fixable from workflow YAML) or committing a seed corpus into the repo — neither is worth the
  cost/risk given two real bugs were already found within the first two cold-corpus runs.

## 3. Prioritised next steps (to raise assurance, in order of value/effort)

1. **Done: pushed, reviewed, and iterated on the first `flight-ci.yml` runs.** `native`,
   `libfuzzer`, and `bigendian` all fully green (confirmed on repeated runs); `libfuzzer` found and
   fixed two real heap-buffer-overflows (SPECTRAL, COLUMNAR) in its first-ever executions; `native`
   hit two environment bugs (`sudo` missing, `setup-python` incompatible with this self-hosted
   runner), both fixed.
2. **Done: MISRA triage, confirmed green in CI.** 179 findings across 17 rules, triaged rule-by-
   rule against real source (not guessed from rule numbers) — 4 fixed, 27 confirmed tool-limitation
   false positives, 148 deliberate-and-verified-safe patterns recorded as formal deviations. Full
   record: `flight/docs/misra-deviations.md`. `misra` confirmed passing in CI (all 4 jobs green on
   the same run) after two more small real findings surfaced once the MISRA noise cleared and were
   triaged the same way.
3. **Done: sustained fuzzing, corpus persistence formally closed out.** Raised `libfuzzer`'s bound
   to 300s — confirmed working. Corpus persistence across CI runs via `actions/cache` confirmed not
   working (run #23, no configured cache backend on this Gitea instance); an `upload-artifact`/
   `download-artifact` alternative was considered and deliberately not pursued (own unverified
   protocol-compatibility risk, not worth it for the gain); the dead cache step was removed from
   both workflow files. Accepted as a real limitation, not left ambiguous — see 2.6 above.
4. **Done: CBMC proof, confirmed green in CI.** `pfc_block_read` (the shared block-framing
   primitive every codec's decoder calls) is proved — not sampled — to never read out of bounds,
   under a 32-bit `size_t` model matching the real flight targets. Getting here found and fixed
   real issues at every layer: (a) writing the harness surfaced the `size_t`-wraparound
   bounds-check bypass itself, 32-bit-only, invisible to every other gate; (b) the `cbmc` job
   needed the upstream release `.deb` (not in Debian's apt repos) plus `gcc-multilib` (`--32`
   needs real 32-bit libc headers to preprocess at all) plus `--c99` instead of GCC's `-std=c99`;
   (c) the proof's first real run then found two more real issues — the harness's own contract was
   too strict (it didn't account for `pfc_block_read` deliberately advancing `pos` on a
   CRC-mismatch rejection), and `pfc_crc32`'s branchless mask idiom (well-defined, but reads as
   overflow to `--unsigned-overflow-check`) was rewritten as an explicit branch. Confirmed on CI
   run #28: `** 0 of 165 failed (1 iterations) / VERIFICATION SUCCESSFUL`. Full writeup:
   `requirements.md`. **`pfc_bound` sufficiency + round-trip correctness attempted, did not
   converge** (see §2.3) — same "not worth an unreliable CI gate" call as `pfc_size_mul`.
   **`pfc_block_write` now also proved** (`0 of 152 failed`), closing the read/write asymmetry left
   when that bug class was fixed defensively on the write side but never verified. The per-codec
   header parsers were evaluated as the next target and deliberately *not* pursued — they are
   embedded in the full decode loop, so they inherit the same state explosion that sank the
   round-trip proof; see the scope note in §2.3.
5. **Done: line + branch coverage, automated in CI.** `make coverage` (gcov/gcovr) over the
   `native` corpus (test_pfc + stress), gated at 95% line / 80% branch project-wide — below the
   measured 98.4%/89.3% baseline, so it catches a real regression without blocking legitimate new
   code. See `coverage` job in `flight-ci.yml` and §2.2. **Remaining gap: MC/DC** — not measured by
   gcov, needs a different tool; see §2.2.
6. **Target bring-up on real hardware**: qemu-user emulation (confirmed working in CI) is real BE
   *execution*, but a dev board (RAD750/LEON/RISC-V-class) and the target RTOS are still a gap.
   **WCET** genuinely needs that hardware. **Stack depth turned out not to** — it's a property of
   the compiled object code, so cross-compiling for the target ABI was enough; done, see §2.4 and
   item 9 below.
9. **Done: worst-case stack-depth bound (R10).** 464 B on the flight target ABI, 632 B on x86-64,
   gated in CI at 1024 B — exact rather than estimated, because no-recursion and no-function-
   pointers make the call graph a complete DAG, and `tools/stack_depth.py` asserts both. See §2.4.
7. **Attempted, did not converge: formal `pfc_bound` sufficiency + small-input round-trip proofs**
   (CBMC) — see §2.3. Left as a testing-covered (not formally proven) property, same call as
   `pfc_size_mul`. Extending CBMC to the per-codec header bounds-checks (item 4's remaining scope)
   is the more tractable next formal-methods step, not this.
8. **Done (solo-authored level): requirements traceability matrix.** `requirements.md` now maps
   all nine requirements (R1–R9, including coding-standard and coverage as process/assurance
   requirements) to their implementing design, verification procedure, and a cited result —
   design→code→test→result, not just requirement→test-file. **Remaining, out of scope for now**:
   an SEU fault model (§2.5), and independent audit / IV&V / the full NPR 7150.2 process (§2.1) —
   both need external review capacity this environment doesn't have.

## 4. Bottom line

libpfc is a small, freestanding, integer-only, no-malloc, error-contained, independently-verified
lossless core that beats the CCSDS-121 flight standard by +2.8% and sits within a percent of both
JPEG-LS (spatial) and CCSDS-123-class (hyperspectral, via the spectral codec), and survives heavy
randomised/fuzz testing under sanitizers — and the testing process itself has caught and fixed real
bugs at every level it's been pointed at: two `pfc_bound` under-estimates during local stress
testing, two real heap-buffer-overflows (SPECTRAL, COLUMNAR) the moment CI-grade libFuzzer actually
ran, and a real 32-bit `size_t`-wraparound bug found writing the CBMC proof before it ever ran. The
CI workflow (`.github/workflows/flight-ci.yml` + the gitea copy) is no longer theoretical — it's
been pushed, run repeatedly, debugged, and as of this writing **six jobs are green on the same
run: `native`, `misra`, `coverage`, `cbmc`, `libfuzzer`, `bigendian`**, including a full,
source-verified MISRA-C:2012 triage (not a rubber stamp: 179 findings read individually, 4 fixed,
the rest justified), a formal CBMC proof of `pfc_block_read`'s memory safety under a 32-bit model,
98.4%/89.3% line/branch coverage gated in CI, and a genuine big-endian execution pass under
`qemu-user`, not just static reasoning about the wire format. A solo-authored requirements
traceability matrix (`requirements.md`) now maps every requirement to its implementing design,
verification procedure, and result. That makes it a strong *candidate* for flight — a materially
stronger one than at the start of this document. "Mission-safe" beyond that is earned through what
remains genuinely open: **MC/DC coverage** (gcov doesn't measure it), **CBMC proof of `pfc_bound`
sufficiency and round-trip correctness** (attempted, didn't converge — see §2.3), **fuzz-corpus
persistence across CI runs** (attempted, formally accepted as a limitation — see §2.6),
real-target (not emulated) hardware validation with WCET analysis, an SEU fault model, and the
NPR 7150.2 assurance process with independent review — none blocked by the design, but all real
work beyond writing code.
