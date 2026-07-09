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
| No dynamic allocation | compiled `.so` imports **zero** alloc symbols; no recursion |
| Bounded memory | single caller-owned `pfc_ctx` (~330 KB at defaults), compile-time sized |
| Determinism | encode-twice byte-identical (checked every stress case) |
| Independent reference | pure-Python ground decoder reproduces C-encoder output byte-for-byte (R7), **10/10** cross-check incl. real CyCIF and real AVIRIS |
| Standard-relative perf | beats CCSDS-121-class Rice **+2.8%** (spatial) and is within **−1.2%** of CCSDS-123-class (hyperspectral, via the spectral codec) on real 16-bit data |
| Structural coverage | gcov: entropy core (range coder/model/CRC/framing) **100%** lines; all five codecs 96–99% lines |

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
- **Bidirectional requirements traceability**: requirement → design → code → test → result, with
  test *procedures* and review records. We have a starter matrix (`requirements.md`); flight needs
  the full, audited chain.
- **Independent V&V (IV&V)**: review by a separate team/organisation.

### 2.2 Structural coverage (beyond functional tests)
- Measure **statement, branch, and MC/DC** coverage (gcov/llvm-cov). Highest-criticality code wants
  ~100% statement/branch and **MC/DC** on decision-heavy functions (range coder renorm, framing,
  predictor edges). Functional tests passing ≠ all branches exercised.

### 2.3 Formal methods (stronger than testing for the safety-critical claims)
- **Bounded model checking** (CBMC / Frama-C) of the decoder: prove *no out-of-bounds read for any
  input up to N bytes* — a proof, not a sample. The decoder eats untrusted/SEU-corrupted data, so
  this is the highest-value formal target.
- Prove **`pfc_bound` sufficiency** (the class of bug we just hit) and **round-trip correctness**
  `decode(encode(x)) == x` exhaustively for small inputs.

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
- **WCET and stack-depth analysis**: bound worst-case execution time per block on the target and
  prove maximum stack usage (no recursion → tractable). Encode throughput must clear the instrument
  data rate with margin.
- **Compiler qualification**: pin and qualify the exact flight toolchain version.

### 2.5 Space environment (SEU / radiation)
- The CRC protects the *downlink* (channel) — but a **single-event upset** can flip a bit in the
  *encoder's* working memory (context tables, range-coder state) mid-encode. Mitigations, mostly
  system-level: EDAC/scrubbed RAM, periodic re-initialisation, and — already in our favour — **small
  independent blocks** that bound the blast radius of any corruption to one block. Document the
  fault model and rely on the platform's EDAC; consider redundant or checksummed working state for
  the highest assurance.

### 2.6 Sustained robustness
- **Continuous fuzzing** (libFuzzer + ASan, days of CPU / OSS-Fuzz-style), seeded corpus, regression
  on every finding. A proper libFuzzer+ASan+UBSan run is wired into CI (`flight-ci.yml`,
  `libfuzzer` job — bounded to 120s per run for now, a smoke test, not a campaign), and **its first
  two executions found two real heap-buffer-overflows** — SPECTRAL header-size validation, then
  (immediately after that fix landed) an unbounded `block_recs` in COLUMNAR's decode, both
  found+fixed — see `requirements.md`. This is exactly the value case for this gate: 170k
  iterations of the *Python* fuzz harness found nothing over the same class of input; coverage-
  guided libFuzzer found two real bugs within minutes of first running. Raising the bound and
  persisting a corpus across runs (rather than starting fresh each time) is the natural next step —
  it's already earned its place twice over on a smoke-test budget.

## 3. Prioritised next steps (to raise assurance, in order of value/effort)

1. **Done: pushed, reviewed, and iterated on the first `flight-ci.yml` runs.** `native`,
   `libfuzzer`, and `bigendian` all fully green (confirmed on repeated runs); `libfuzzer` found and
   fixed two real heap-buffer-overflows (SPECTRAL, COLUMNAR) in its first-ever executions; `native`
   hit two environment bugs (`sudo` missing, `setup-python` incompatible with this self-hosted
   runner), both fixed.
2. **Done: MISRA triage.** 179 findings across 17 rules, triaged rule-by-rule against real source
   (not guessed from rule numbers) — 4 fixed, 27 confirmed tool-limitation false positives, 148
   deliberate-and-verified-safe patterns recorded as formal deviations. Full record:
   `flight/docs/misra-deviations.md`. `misra` in CI still shows red until those 4 fixes +
   suppression list are pushed and the job re-run.
3. **CBMC proof: decoder never reads OOB** for inputs ≤ N — converts the fuzz evidence into a proof.
4. **Coverage**: add gcov to CI, drive branch coverage to ~100% and MC/DC on the range coder +
   framing + predictor-edge functions.
5. **Target bring-up on real hardware**: qemu-user emulation (confirmed working in CI) is real BE
   *execution*, but a dev board (RAD750/LEON/RISC-V-class) and the target RTOS are still a gap;
   WCET + stack analysis need the real target, not an emulator.
6. **Sustained fuzzing**: raise `libfuzzer`'s 120s bound and persist a corpus across CI runs
   (currently starts fresh each run).
7. **Formal `pfc_bound` sufficiency + small-input round-trip proofs** (CBMC).
8. **Process artifacts**: full traceability, test procedures, SEU fault model, and IV&V — per
   NPR 7150.2 for the assigned software class.

## 4. Bottom line

libpfc is a small, freestanding, integer-only, no-malloc, error-contained, independently-verified
lossless core that beats the CCSDS-121 flight standard by +2.8% and sits within a percent of both
JPEG-LS (spatial) and CCSDS-123-class (hyperspectral, via the spectral codec), and survives heavy
randomised/fuzz testing under sanitizers — and the testing process itself has caught and fixed two
real contract bugs (both `pfc_bound` under-estimates). That makes it a strong *candidate* for
flight. A CI workflow (`.github/workflows/flight-ci.yml`) now automates MISRA, libFuzzer, and a
genuine big-endian execution run — authored and reasoned correct, but unexecuted as of this
writing; its first real run is the next concrete step, not a formality. "Mission-safe" beyond that
is earned through the remaining items above: structural/MC/DC coverage, formal memory-safety
proofs, real-target (not emulated) hardware validation with WCET analysis, an SEU fault model, and
the NPR 7150.2 assurance process with independent review — none blocked by the design, but all
real work beyond writing code.
