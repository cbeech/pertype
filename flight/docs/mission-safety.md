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
- **Remaining gap: MC/DC.** gcov measures statement and branch coverage, not modified
  condition/decision coverage — a compound boolean like `a && b || c` can hit every branch outcome
  without exercising every condition's independent effect. Highest-criticality code (range coder
  renorm, framing, predictor edges) wants MC/DC for a real DO-178C-spirit claim; this needs a
  different tool (e.g. `llvm-cov` with `-fcoverage-mcdc`, or a dedicated MC/DC analyzer) and is not
  yet attempted.

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
   converge** (see §2.3) — same "not worth an unreliable CI gate" call as `pfc_size_mul`. Remaining
   scope: extend to the per-codec header parsers that also touch untrusted fields (a bounds-only
   proof, the SAT-friendly case this toolchain has proven it can handle).
5. **Done: line + branch coverage, automated in CI.** `make coverage` (gcov/gcovr) over the
   `native` corpus (test_pfc + stress), gated at 95% line / 80% branch project-wide — below the
   measured 98.4%/89.3% baseline, so it catches a real regression without blocking legitimate new
   code. See `coverage` job in `flight-ci.yml` and §2.2. **Remaining gap: MC/DC** — not measured by
   gcov, needs a different tool; see §2.2.
6. **Target bring-up on real hardware**: qemu-user emulation (confirmed working in CI) is real BE
   *execution*, but a dev board (RAD750/LEON/RISC-V-class) and the target RTOS are still a gap;
   WCET + stack analysis need the real target, not an emulator.
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
