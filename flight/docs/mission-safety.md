# libpfc — mission safety: where we are, and what "flight-qualified" still requires

Honest assessment. Passing an aggressive test suite is **necessary but not sufficient** for flying
software on a spacecraft. This document separates what is *evidenced now* from what a real flight
qualification (NASA NPR 7150.2 software-assurance process, and the spirit of DO-178C structural
coverage) still demands.

## 1. What is evidenced today

| Property | Evidence |
|----------|----------|
| Lossless round-trip | 70 unit + **15 063 randomised/edge cases** across all 4 codecs + real CyCIF; 0 failures |
| No expansion (`pfc_bound`) | property-checked on adversarial inputs — **a real under-estimate bug was found and fixed** here |
| Error containment | bit-flip + truncation tests, **170 000 fuzz iterations**, all under **ASan + UBSan**, no OOB/crash |
| No dynamic allocation | compiled `.so` imports **zero** alloc symbols; no recursion |
| Bounded memory | single caller-owned `pfc_ctx` (329 KB at defaults), compile-time sized |
| Determinism | encode-twice byte-identical (checked every stress case) |
| Independent reference | pure-Python ground decoder reproduces C-encoder output byte-for-byte (R7) |
| Standard-relative perf | beats CCSDS-121-class Rice +1.5% on real 16-bit data |

This is a high-quality, well-tested core — a credible *foundation*. It is not yet flight-qualified.

## 2. The gap to flight qualification

### 2.1 Process & standards (the bulk of real qualification)
- **NPR 7150.2 / NASA-STD-8739.8** software assurance: classify the software (likely Class B/C),
  then satisfy the required activities — plans, reviews, audits, configuration management.
- **Coding-standard compliance report**: MISRA-C:2012 + JPL *Power of Ten*, run with a qualified
  analyzer (Coverity / LDRA / Polyspace / cppcheck-MISRA), every finding resolved or formally
  deviated. Wired as `make misra`; **not yet run** (no analyzer in this environment).
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
  decoder, but have **not run on big-endian hardware** (no cross toolchain/qemu here). Validate R4
  end-to-end there.
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
  on every finding. 170 k iterations is a good smoke test, not a campaign.

## 3. Prioritised next steps (to raise assurance, in order of value/effort)

1. **CBMC proof: decoder never reads OOB** for inputs ≤ N — converts the fuzz evidence into a proof.
2. **MISRA + coverage**: run `make misra`; add gcov, drive branch coverage to ~100% and MC/DC on the
   range coder + framing + predictor-edge functions.
3. **Target bring-up**: cross-compile (BE PowerPC / LEON / RISC-V), run the full suite under qemu and
   on a dev board; WCET + stack analysis.
4. **Sustained fuzzing** with a persisted corpus; wire into CI.
5. **Formal pfc_bound sufficiency + small-input round-trip proofs** (CBMC).
6. **Process artifacts**: full traceability, test procedures, SEU fault model, and IV&V — per
   NPR 7150.2 for the assigned software class.

## 4. Bottom line

libpfc is a small, freestanding, integer-only, no-malloc, error-contained, independently-verified
lossless core that already beats the CCSDS-121 flight standard and survives heavy randomised/fuzz
testing under sanitizers — and the testing process itself has already caught and fixed a real
contract bug. That makes it a strong *candidate* for flight. "Mission-safe" is then earned through
the items above: static-analysis compliance, structural coverage, formal memory-safety proofs,
on-target/WCET validation, an SEU fault model, and the NPR 7150.2 assurance process with independent
review — none of which are blocked by the design, but all of which are real work beyond writing code.
