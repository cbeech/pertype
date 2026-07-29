# Overnight run — flight-core assurance slate (2026-07-28)

Branch: `flight-core`. Push target: `origin` (gitea) only, never github.

The previous run's report was archived to `OVERNIGHT-REPORT-2026-06-26.md`.

**User-authorized deviation from the `overnight` skill's "never push" hard rule:** the user
explicitly chose "commit + push to gitea as I go" over a stated "commit locally, don't push"
alternative, and re-confirmed at kickoff. This was flagged to them before they left. Every push is
gated on `make check` passing first.

## Queue

| # | Task | Status |
|---|------|--------|
| 0 | Sustained fuzzing | running — corpus 1 473, **0 crashes** |
| 1 | MC/DC coverage measurement + CI gate | ✅ done — **55.65% → 76.52%** |
| 2 | Worst-case stack-depth analysis | ✅ done — 464 B on flight target |
| 3 | SEU fault-injection harness | ✅ done — **found a real hang bug** |
| 4 | CBMC proof extension | ✅ done — `pfc_block_write` proved |
| 5 | *(unplanned)* Fix the SEU-induced range-coder hang | ✅ done + regression test |

All five planned items are gaps this repo's own `flight/docs/mission-safety.md` names as open —
none is invented scope. Item 5 was not planned; it exists because task 3 found a real defect.

**Headline: the night found and fixed a genuine bug** — an SEU-induced infinite loop in the range
coder, which was also a latent JPL Power-of-Ten Rule 2 violation. Details under task 3.

## Outcomes

<!-- One section per task, appended AS IT COMPLETES, never batched at the end. -->

### Task 0 — sustained fuzzing
**Status: running (segment 2).** Started at 12 workers; after ~35 min (~7 core-hours, **0 crashes**,
901 corpus entries) it was starving the other four tasks of CPU, so it was restarted CPU-capped at
4 workers seeded from that same corpus. Honest caveat for the writeup: this is **two segments, not
one continuous run**. Corpus and crash artifacts persist to the session scratchpad
(`scratchpad/fuzz/{corpus,artifacts}`) — which incidentally sidesteps, for this run, the CI
corpus-persistence limitation documented in `mission-safety.md` §2.6.

**Interim: corpus grown 901 → 1 743, zero crash artifacts.**

Two worker logs (`fuzz-1`, `fuzz-4`) contain the single line `Segmentation fault` and nothing else.
Investigated rather than assumed: both are 19 bytes with **no libFuzzer startup banner**, meaning
the process died before executing any input — a worker startup failure, not a crash in the target
(libFuzzer writes a `crash-*` artifact plus an ASan report for those, and none exist). Confirmed
definitively by replaying the **entire 1 583-entry corpus** through the current build in a single
process: all inputs pass, exit 0. Worth recording as a caveat on the fuzzing evidence — effective
worker count was lower than requested for part of the run — but not a defect.

### Task 2 — worst-case stack-depth analysis ✅ DONE
**This item was mis-scoped in the roadmap and that is the main finding.** §2.4 previously bundled
stack depth with WCET as "needs the real target hardware". That is wrong: frame sizes and the call
graph are properties of the *compiled object code*, so cross-compiling for the target ABI is
sufficient. Only timing genuinely needs silicon. So an item listed as blocked was in fact
completable tonight, with no hardware.

New `tools/stack_depth.py` computes the bound by longest-path over GCC `-fstack-usage` frames, with
call edges recovered from `objdump -dr`. Measured:

| Target | `pfc_encode` | `pfc_decode` | Worst |
|---|---|---|---|
| x86-64 `-O2` | 560 B | 632 B | 632 B |
| **PowerPC BE 32-bit (flight ABI) `-O2`** | 384 B | **464 B** | **464 B** |

The bound is **exact, not estimated**, because no-recursion makes the call graph a DAG and
no-function-pointers makes it complete — and the tool *asserts* both rather than assuming, failing
loudly if either JPL Power-of-Ten rule ever regresses. So this job doubles as a guard on those
rules.

Three caveats found and documented rather than buried: (1) `.su` frames exclude the return address
pushed by `call`; (2) one `memset` edge into libc has no `.su` entry and is reported as
unresolved-external instead of silently counted as zero; (3) GCC labels the two entry frames
`dynamic,bounded` on x86-64 but `static` on PowerPC — another reason to quote the flight-target
number, not the host one.

Wired as `make stackdepth` / `make stackdepth-ppc` + a `stackdepth` CI job on both workflow copies,
gated at `STACK_BUDGET`=1024 B. Docs: new R10 requirement, traceability row, `requirements.md`
section, §2.4 rewritten, roadmap item 6 corrected.

### Bonus finding — the optional-tool skip guards never skipped
`make coverage` failed with `gcovr: No such file or directory` *immediately after printing its own
"gate skipped" message*. Root cause: the idiom

```make
@command -v foo >/dev/null 2>&1 || { echo "... (CI gate skipped)"; exit 0; }
```

does not skip anything — `exit 0` ends only that **recipe line's** shell, and make then runs the
next line regardless. Five gates used it (`misra`, `cbmc`, `coverage`, `mcdc`, `stackdepth-ppc`);
two predate tonight. The bug stayed latent because CI always has the tools installed — i.e. it only
ever misfired in exactly the situation the guard exists for, and the log it produced ("skipping…"
then dying inside the skipped gate) was actively misleading.

Fixed by detecting each tool once at parse time and wrapping the recipe in a make conditional,
which genuinely short-circuits. Verified in a container with all five tools deliberately absent:
every gate exits 0 having run nothing, while a gate whose toolchain *is* present still executes.

### Task 1 — MC/DC coverage ✅ DONE (headline finding of the night)
clang 18's `-fcoverage-mcdc` (bookworm tops out at clang 16, so this needs the upstream
`apt.llvm.org` repo — the same shape of problem as the CBMC `.deb`).

**First measurement: 55.65% MC/DC (64 of 115 conditions)** — against 98.4% line and 89.4% branch on
the *identical corpus*. That gap is the whole point of MC/DC and it was real here, not theoretical:
worst files `pfc_spectral.c` 34.8%, `pfc_columnar.c` 36.4%, `pfc.c` 45.0%.

Added `test/test_mcdc.c` targeting condition *independence* rather than behaviour — for an N-way
`||` chain you need N+1 vectors (all-false, then each condition true alone), which no existing test
provided. **68 assertions, all passing. Result: 55.65% → 76.52% (88/115).**

| file | before | after |
|------|--------|-------|
| `pfc.c` | 45.0% | **100%** |
| `pfc_frame.c` | 50.0% | **100%** |
| `pfc_seq.c` | 66.7% | **80.0%** |
| `pfc_columnar.c` | 36.4% | **63.6%** |
| `pfc_spectral.c` | 34.8% | **60.9%** |

These are genuine robustness tests, not metric-chasing. The magic-byte cases would catch a mistyped
index (`s[2]` checked twice, leaving a header byte unvalidated) that every existing behavioural test
misses, because those only ever corrupt the whole header at once. Several wire-header clauses guard
*liveness* rather than validity — a zero `block` / `block_recs` / `band` field would leave the
corresponding decode loop unable to advance.

Shipped as `make mcdc` + `tools/mcdc_gate.py` + an `mcdc` CI job on both workflows, and
`make check` now runs the new tests too. **The gate is an explicit ratchet (`MCDC_MIN`, raised
55→62→73 as coverage climbed), not a compliance claim** — DO-178C wants ~100%.

**27 conditions remain, and they are a harder class than what was closed.** The residual
`pfc_image.c` / `pfc_arith.c` conditions live inside the per-sample predictor and the range coder's
renormalisation loop, where a condition's independent effect depends on coder state accumulated
over many prior symbols rather than on a directly-constructible input. Closing them needs
state-seeded tests below the public API or a solver-assisted search — not more crafted headers.
Documented as such rather than papered over.

### Task 3 — SEU fault injection ✅ DONE — and it found a REAL BUG (a hang)
Harness injects via linker `--wrap` on `pfc_resid_encode`, so `src/` compiles **completely
unmodified** — no test-only `#ifdef` hooks in MISRA-reviewed flight code.

**Measured (7 200 uniform trials, 64×64 16-bit, 4 bands):** 99.9% CLEAN, 0.1% SILENT,
**0.0% DETECTED**. The zero is the headline: not one encoder-side upset was caught by anything.
That *confirms* rather than contradicts §2.5 — the CRC is computed after the corruption — but shows
there is no incidental detection either. Encoder-side SEU is a wholly silent failure mode.

Two further results the prose did not contain:
- **Containment holds, but "contained" ≠ "small".** Across **175 silent corruptions, none crossed
  a band boundary** — block-independence validated with evidence. But within a band damage is
  near-total: worst **1 017 corrupted samples of a 1 024-sample band**. One flipped bit can
  silently destroy an entire band.
- **Risk is inverse to region size, which makes hardening cheap.** Stratified injection (300
  trials/region): `tot[]` 20.3%, `mant[][]` 19.7%, `freq[][]` 7.7%, `bias_*[]` 7.0% silent — versus
  `scratch[]` 0.7% and `xform[]` 0.0%, which are 99% of the memory but overwritten before use.
  **Actionable: the four model regions total 3 012 B — 0.91% of the 330 KB workmem — and carry
  essentially all the risk.** Protecting ~3 KB with EDAC or a checksum addresses the dominant
  failure mode at ~1/100th the cost of hardening the whole context. That is a realistic ask of a
  flight platform in a way "harden all working memory" is not — and it is a concrete
  recommendation §2.5 previously could not make.

#### 🔴 The real bug: SEU-induced infinite loop in the range coder
Adding stratified sampling made the harness hit the model tables far more often — and it **hung**.
Not slowness: 99% CPU with zero progress. Diagnosed rather than guessed, and reproduced minimally
(`hang_repro.c`, outside the harness entirely, driving the range coder directly):

> A flipped bit that zeroes a `freq[][]` entry makes `pfc_rc_encode` compute
> `range = (range/tot) * 0 == 0`. Then `low ^ (low + 0) == 0` is permanently `< PFC_RC_TOP`, and
> `range <<= 8` can never restore it. **`pfc_rc_enc_renorm` spins forever.**

On a spacecraft that is a hung compression task and a watchdog reset — **strictly worse** than the
silent corruption §2.5 anticipated. It was also a latent **JPL Power-of-Ten Rule 2 violation**: that
`while` loop had no statically provable bound, and this is the concrete proof it could run unbounded.

`freq >= 1` is an invariant on healthy state (`pfc_model_reset` seeds ≥1, `pfc_model_rescale`'s
`(f+1)>>1` never reaches 0, `pfc_model_update` only adds), so it is unreachable without memory
corruption — which is exactly the flight fault model.

**Fixed** by bounding both renorm loops with `PFC_RC_RENORM_MAX` (8; legitimate renormalisation
needs ≤4 since each iteration shifts 8 bits of a 32-bit value). The encoder now sets `overflow`,
which every codec **already** handles by falling back to store-raw — so a corrupted model costs one
block's compression instead of hanging. The decoder simply stops renormalising, leaving the existing
CRC/containment machinery to report it.

**Verified no behaviour change:** all 146 tests pass and every compression ratio is byte-identical
(166.12×, 123.76×, 261.44×, 10.14× …). Regression test `test_renorm_bound` added.

### Task 4 — CBMC ✅ DONE, but on a deliberately different target than planned
The roadmap named the four per-codec header parsers. I attempted that framing and **rejected it on
evidence**: each parser is embedded in its codec's full decode loop, so any proof drags in the range
coder and adaptive model — exactly the state explosion that made the earlier `pfc_bound`/round-trip
proofs fail to converge. Proving them would have required bounding `count`/`height` so tightly the
result would say almost nothing.

Pivoted to **`pfc_block_write`**, which is the better target on the merits: bounds-only arithmetic,
structurally identical to the `pfc_block_read` proof that *did* converge, and it closes a real
asymmetry — when the `size_t`-wraparound bug was fixed, the write side was changed "defensively"
and then never verified. It also guards a genuine flight-side threat: `plen` there derives from the
range coder's output, and the *encoder* is what runs on the 32-bit spacecraft hardware where the
wraparound is reachable.

**`0 of 152 failed / VERIFICATION SUCCESSFUL`** under `--32`, with `block_read` re-verified as a
regression (`0 of 165`). Both now run in `make cbmc`. The proved contract deliberately differs from
the read side: on rejection `pos` must be *completely unchanged* (a rejected write must not leave a
hole), whereas `block_read` intentionally advances on CRC mismatch to stay in framing sync.
