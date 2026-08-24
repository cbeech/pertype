# Roadmap — pertype / libpfc

**Current state:** One repository, one branch. **`master`** carries both workstreams since the
2026-08-19 merge. `pertype` is released and published (v0.1.0 on GitHub Releases, PyPI, crates.io;
dual-licensed AGPL-3.0 + commercial), `TODO.md` 68 done / 3 low-priority open (all features, so all
out of scope under D1), productization plan COMPLETE with the remaining channels explicitly
deferred, data-type sweep backlog exhausted. `libpfc` lives in `flight/` — a freestanding C99
flight core with 8 CI jobs, CBMC proofs, a traceability matrix and an independent Python ground
decoder — and is **now public on GitHub** under its Apache-2.0 carve-out. `flight-core` is retired
on both remotes.

**Target:** `libpfc` stops living on an unmerged branch and becomes public Apache-2.0 code whose
documents don't overstate what's been measured; `pertype` receives only hygiene. Agreed at
roadmap review: **libpfc gets the real work, pertype gets bug-fix attention only.**

> ## Track A is complete
>
> Every Track A goal — G0.1, G0.2, G1.1, G1.2, G1.3, G2.1, G3.1, G3.2, G3.3, G3.4a–d and G4.1 —
> plus Track B's R3 has landed, in commits `d8f2ad3` … `b4a6d15`. Each goal below carries a
> **Status** line naming the commit that closed it. R2, R4 and R5 are deferred or shelved under D2
> and are *not* oversights. **Nothing in Track A is awaiting work**; the open items are the
> follow-ups flagged in the M3 note below and the human-gated items under "Questions for you".

**Last surveyed:** 2026-08-24 · **Decisions recorded:** 2026-08-15 review

## Decisions taken at review (these override the earlier draft)

| # | Decision |
|---|---|
| D1 | Both workstreams stay in scope, but **pertype gets bug-fix attention only** — no new features. Cuts former Track B R1 (publish the sweep) and R6 (Tier 3). |
| D2 | libpfc's audience is **speculative** — build as if, but no funded opportunity. R5 (hardware) is explicitly shelved; R2/R4 kept as cheap credibility. |
| D3 | **Do the MC/DC work** (G3.4) despite my recommendation to defer — it is the weakest structural number and worth closing. |
| D4 | **Measure exactly which conditions are uncovered before splitting G3.4** — my "L, unsplittable" label was inferred and is probably wrong (see G3.4). |
| D5 | The Linux workstation (AVIRIS pool) **is available** — G3.1 is scheduled there, early. |
| D6 | **Merge `flight-core` into `master`**, keeping `flight/` as a subdirectory with its Apache-2.0 licence boundary. Not a separate repo. |
| D7 | Merging **publishes libpfc on public GitHub** — confirmed as intended. |
| D8 | **Merge first; do all of M1 after.** Recorded as a deliberate choice: this publishes `flight/README.md` while it still asserts a containment property measured false (see M1 risk note). |

---

## Track A — Complete the project

### M0 — Merge and publish (do this first, per D8)
**Outcome:** `libpfc` lives on `master`, is public on GitHub, and the long-lived branch that hid six
weeks of research no longer exists.

/goal Merge the flight-core branch into master, resolving the single expected conflict in the sweep document.
- **ID:** G0.1
- **Why:** `flight-core` has never merged; the divergence already caused a real failure (commit
  `6f5db50`, three completed sweep targets, invisible on master for ~6 weeks). Merging removes the
  cause rather than guarding against the symptom.
- **Done when:** `master` contains `flight/`, `git rev-list --count master..flight-core` is 0, and
  `make check` passes from a clean checkout of merged `master`.
- **Touches:** whole tree; conflict confined to `docs/data-type-opportunities.md`
- **Depends on:** none
- **Size:** S — a trial merge was run: **exactly one conflicted file**,
  `docs/data-type-opportunities.md`, because a cherry-pick (`8ca41f6`) duplicated flight-core's
  `6f5db50` under a different SHA. Master's side is a **superset** (same three entries plus OTLP),
  so the resolution is "take master's version". No other file conflicts.
- **Basis:** verified in repo (trial merge executed and aborted)
- **Status:** **Done** (`d8f2ad3`). Merged with `flight/` kept as a subdirectory; the single
  expected conflict in `docs/data-type-opportunities.md` was resolved by taking master's side, as
  planned. `make check` passes on the merged tree.

/goal Retire the flight-core branch and record the post-merge branch and push policy.
- **ID:** G0.2
- **Why:** After the merge, "flight-core pushes to gitea only" is obsolete and actively misleading
  — flight work now goes to public GitHub via master. Leaving the old rule in `HANDOFF.md` and
  memory invites the wrong behaviour. **Critically, the replacement rule must not over-generalise:**
  `research/llm-vram` is a third branch that is explicitly **gitea-only, never github** (private
  research). Verified isolated from both master and flight-core — no llm-vram commits are in the
  merge set — but a "push everything to both remotes" reflex after this change would expose it.
- **Done when:** `flight-core` is deleted or archived on both remotes; the push policy is stated in
  one place a fresh session would read; and that statement **explicitly preserves the
  `research/llm-vram` gitea-only exception** rather than describing a blanket rule.
- **Touches:** `HANDOFF.md`, branch refs
- **Depends on:** G0.1
- **Size:** S
- **Basis:** verified in repo
- **Status:** **Done.** `flight-core` is deleted on both remotes (`git ls-remote --heads`
  confirms: github has `master` only; origin has `master` + `research/llm-vram`). The post-merge
  push policy is stated at the top of `HANDOFF.md` and **preserves the `research/llm-vram`
  gitea-only exception explicitly** rather than describing a blanket rule.

### M1 — Documentation tells the truth
**Outcome:** No published file claims a safety property the project's own harnesses measured false.

> **Risk accepted under D8:** this milestone now runs *after* publication. G1.1 in particular
> corrects a claim already known to be false, so between M0 and G1.1 the public README overstates
> error containment. Recorded as deliberate, not oversight. Land G1.1 promptly to keep the window
> short.

/goal Correct the error-containment claim in flight/README.md so it no longer asserts that a corrupted frame loses exactly one block for all codecs.
- **ID:** G1.1
- **Why:** `flight/README.md:25–27` states "A corrupted or truncated downlink frame loses exactly
  one block" with no exception noted. `make containment` measures 6 of 6 bands damaged for SPECTRAL
  from one corrupt block, with an IMAGE control confining damage to 1 band.
- **Done when:** The README states per-codec containment, names SPECTRAL as the exception, and
  references `docs/mission-safety.md` §2.5.1; `grep -ci "propagat\|refresh band" flight/README.md`
  is non-zero (currently 0).
- **Touches:** `flight/README.md`
- **Depends on:** G0.1
- **Size:** S
- **Basis:** verified in repo
- **Status:** **Done** (`4f9730e`). `flight/README.md` now states per-codec containment, names
  SPECTRAL as the exception and points at `docs/mission-safety.md` §2.5.1. The window opened by D8
  was closed the same day as the merge, as the risk note asked.

/goal Refresh the stale factual claims in flight/README.md against current measured values.
- **ID:** G1.2
- **Why:** README says workmem is "262 960 B at defaults"; the suite prints 330 708 B. It also calls
  the big-endian run and MISRA/libFuzzer reports outstanding — all are green CI jobs.
- **Done when:** The stated workmem equals what `./build/test_pfc` prints, and no sentence describes
  an already-green gate as remaining.
- **Touches:** `flight/README.md`
- **Depends on:** G1.1
- **Size:** S
- **Basis:** verified in repo
- **Status:** **Done** (`4f9730e`). Workmem corrected to the measured 330 708 B; the sentence
  calling big-endian, MISRA and libFuzzer outstanding is gone — all three are green CI jobs.

/goal Audit every universal safety claim across the flight documents for the same class of overstatement.
- **ID:** G1.3
- **Why:** The SPECTRAL overstatement sat unverified from the codec's inception and was found only
  by deliberately looking. Other "never / always / exactly one" claims may be similarly unbacked.
- **Done when:** Every universal quantifier in a safety claim across `flight/README.md`,
  `flight/docs/requirements.md` and `flight/docs/mission-safety.md` is either tied to a named
  harness/proof or annotated with its exception; the checked list is recorded in `requirements.md`.
- **Touches:** `flight/README.md`, `flight/docs/requirements.md`, `flight/docs/mission-safety.md`
- **Depends on:** G1.2
- **Size:** M
- **Basis:** inferred
- **Status:** **Done** (`4f9730e`). A universal safety-claim audit table was added to
  `requirements.md`, tying every universal quantifier to a named harness/proof or to its
  documented exception.

### M2 — Distribution decision (lightweight, per D2)
**Outcome:** How `libpfc` reaches a user is written down; no release is cut while the audience is
speculative.

/goal Document libpfc's licence boundary and distribution route in one place.
- **ID:** G2.1
- **Why:** `flight/` is Apache-2.0 carved out of the AGPL root explicitly so government/NASA users
  can deploy it, but no document states how they would obtain it. Post-merge the answer is "clone
  master, use flight/" — that needs saying, along with what the AGPL boundary means for a consumer.
- **Done when:** `flight/README.md` states the licence boundary and how to consume `flight/`
  standalone, and says explicitly that no separate tagged release exists yet and why.
- **Touches:** `flight/README.md`
- **Depends on:** G0.1
- **Size:** S
- **Basis:** verified in repo
- **Status:** **Done** (`4f9730e`). `flight/README.md`'s Licensing section now covers the
  Apache-2.0 / AGPL boundary, how to consume `flight/` standalone, and why no separate tagged
  release exists yet.

> **Deferred (D2):** cutting a tagged `libpfc` release artifact. Revisit if an integrator appears.

### M3 — Close the flight evidence gaps that don't need hardware
**Outcome:** Everything verifiable without a dev board has been verified, so the remaining gaps are
unambiguously the externally-blocked ones.

/goal Re-measure the SPECTRAL refresh-band compression cost on real AVIRIS hyperspectral data.
- **ID:** G3.1
- **Why:** §2.5.1 publishes a cost curve (refresh=4 → +14.68%) measured on a synthetic cube and
  flagged unverified. The same feature measured on a weakly-correlated cube gave +0.88% — an
  order-of-magnitude swing from test data alone — so this is the figure most likely to mislead
  someone choosing an interval, and it is now public.
- **Done when:** §2.5.1's table reports intervals measured on real AVIRIS, the synthetic-only caveat
  is replaced, and containment is re-confirmed.
- **Touches:** `flight/docs/mission-safety.md`, `flight/docs/requirements.md`
- **Depends on:** G0.1
- **Size:** M — originally scheduled on the Linux workstation (`~/sci_data`), but the public
  AVIRIS Indian Pines scene was obtained via the Internet Archive Wayback Machine and measured here.
- **Basis:** verified in repo
- **Status:** **Done.** Measured on real AVIRIS Indian Pines (200 bands, 145×145, uint16). The
  synthetic +14.68% figure for refresh=4 was an overestimate: the same interval costs **+3.96%**
  on this real scene. Full price curve and updated guidance are in `flight/docs/mission-safety.md`
  §2.5.1 and `flight/docs/requirements.md`.

/goal Extend SEU fault injection to a multi-bit / burst upset model.
- **ID:** G3.2
- **Why:** §2.5 states the model is single-bit only and calls that "a start, not a complete fault
  model". Real upsets include multi-bit events in adjacent cells; `make seu` already has the
  injection machinery.
- **Done when:** `make seu` supports a burst mode of N adjacent bits, results for at least one burst
  width are in §2.5 beside the single-bit figures, and the containment conclusion is confirmed or
  corrected for bursts.
- **Touches:** `flight/test/seu_inject.c`, `flight/docs/mission-safety.md`
- **Depends on:** none
- **Size:** M
- **Basis:** verified in repo
- **Status:** **Done** (`f1eb7b6`). `test/seu_inject.c` flips N adjacent bits via `SEU_BURST=N`;
  single-bit and 8-bit-burst results are both recorded in §2.5.

/goal Measure SEU behaviour for the FLOAT codec rather than inferring it from COLUMNAR.
- **ID:** G3.3
- **Why:** §2.5 records FLOAT as "not separately measured (shares COLUMNAR's implementation)" — an
  inference. The multi-codec run already showed shared implementation does not imply shared
  behaviour: COLUMNAR's `xform[]` carries risk IMAGE's does not.
- **Done when:** `make seu` includes a FLOAT fixture and §2.5 reports its outcome distribution and
  containment result directly.
- **Touches:** `flight/test/seu_inject.c`, `flight/docs/mission-safety.md`
- **Depends on:** G3.2
- **Size:** S
- **Basis:** verified in repo
- **Status:** **Done** (`f1eb7b6`). A FLOAT fixture was added, so all five codecs are now measured
  directly rather than FLOAT being inferred from COLUMNAR.

> **G3.4 has been measured and split (D4 discharged).** A per-region `llvm-cov --show-mcdc` run
> produced the exact 27 uncovered condition-pairs with file:line. The result contradicts my earlier
> "L, unsplittable, state-seeded" label — there are **four classes**, of which only three
> conditions are genuinely state-dependent:
>
> | class | n | conditions |
> |---|---|---|
> | **A** — parameter/header validation | 14 | `pfc_spectral.c:199` (6), `pfc_image.c:383` (3), `:309` (2), `pfc_columnar.c:25` (2), `pfc_seq.c:73` (1) |
> | **B** — `pfc_size_mul` capacity guards | 8 | `pfc_spectral.c:277` (3), `pfc_image.c:391` (2), `pfc_columnar.c:110` (1), `pfc_seq.c:144` (1), `pfc_internal.h:183` (1) |
> | **C** — store-raw fallback | 2 | `pfc_columnar.c:73`, `pfc_seq.c:111` |
> | **D** — genuinely state-dependent | 3 | `pfc_arith.c:39`/`:118` renorm underflow (C3), `pfc_image.c:121` gradient tie-break (C4) |

/goal Cover the 14 parameter- and header-validation conditions using the existing crafted-input technique.
- **ID:** G3.4a
- **Why:** Class A is the same shape as the work that already took `pfc.c` and `pfc_frame.c` to
  100%. The gap is specific and embarrassing in a good way: `test_mcdc.c` covers *decode* headers
  for SEQ/COLUMNAR/SPECTRAL but **never covers IMAGE decode headers at all, and never covers the
  encode-side parameter guards for any codec** — `pfc_spectral.c:199` alone is 6 of the 27.
- **Done when:** `make mcdc` shows `pfc_spectral.c`, `pfc_image.c`, `pfc_columnar.c` and
  `pfc_seq.c` each improved, total MC/DC ≥ 88%, `MCDC_MIN` ratcheted just below, `make check` green.
- **Touches:** `flight/test/test_mcdc.c`, `flight/Makefile`
- **Depends on:** G0.1
- **Size:** M
- **Basis:** verified in repo (measured condition list)
- **Status:** **Done** (`8402e5a`). Added encode-side parameter-guard tests for SPECTRAL, IMAGE,
  COLUMNAR and SEQ plus the missing IMAGE decode-header test; `test_mcdc.c` grew to 97 assertions.
  ⚠️ The `MCDC_MIN` ratchet in this goal's done-when was **not** moved — see the follow-up note
  under M3.

/goal Decide and document the fate of the 8 pfc_size_mul capacity-guard conditions.
- **ID:** G3.4b
- **Why:** **These are probably not testable on the CI host at all.** `requirements.md` already
  states IMAGE/COLUMNAR/SEQ's `pfc_size_mul` guards were "fixed defensively for the same
  32-bit-reuse reason… though their two/three-factor products can't overflow 64 bits given their
  existing bounds" — i.e. the `!pfc_size_mul(...)` branch is **unreachable by construction on a
  64-bit machine model**. Chasing them in normal CI is wasted effort; only SPECTRAL's four-factor
  product can overflow 64 bits (and already has a regression test).
- **Done when:** Each of the 8 is classified as either (a) reachable and covered, or (b) unreachable
  on a 64-bit model with the reason recorded in `requirements.md`; if a 32-bit MC/DC build is judged
  worthwhile, that decision is written down rather than left implicit.
- **Touches:** `flight/docs/requirements.md`, possibly `flight/Makefile`
- **Depends on:** G3.4a
- **Size:** S
- **Basis:** verified in repo (the unreachability claim is the project's own, in `requirements.md`)
- **Status:** **Done** (`8402e5a`). All 8 classified: unreachable on a 64-bit model by
  construction, with the reason recorded in `requirements.md`, except SPECTRAL's four-factor
  product, which is reachable and already regression-tested.

/goal Cover the 2 store-raw fallback conditions with deliberately incompressible input.
- **ID:** G3.4c
- **Why:** `pfc_columnar.c:73` and `pfc_seq.c:111` are the `e.pos >= raw_bytes` half of the
  store-raw decision — the encoder produced output no smaller than the input. Existing random-data
  tests hit the *decision* but not this condition independently.
- **Done when:** Both conditions are covered and `make check` stays green.
- **Touches:** `flight/test/test_mcdc.c`
- **Depends on:** G3.4a
- **Size:** S
- **Basis:** verified in repo
- **Status:** **Done** (`8402e5a`). Store-raw fallback tests for SEQ and COLUMNAR force each half
  of the decision independently.

/goal Resolve the 3 genuinely state-dependent conditions, covering or formally excluding them.
- **ID:** G3.4d
- **Why:** `pfc_arith.c:39`/`:118` C3 is the range coder's underflow branch
  (`range = (0 - low) & (BOT-1)`), reachable only with a specific low/range relationship built up
  over many symbols; `pfc_image.c:121` C4 is the third tier of the gradient sign tie-break
  (`q1==0 && q2==0 && q3<0`). These are the only ones matching my original "needs coder state"
  description.
- **Done when:** Each is either covered by a state-seeded test below the public API, or recorded in
  `requirements.md` as not-unit-testable with the reason — the same treatment already given to the
  `pfc_size_mul` CBMC non-convergence.
- **Touches:** `flight/test/`, `flight/docs/requirements.md`
- **Depends on:** G3.4c
- **Size:** M
- **Basis:** verified in repo
- **Status:** **Done** (`8402e5a`). The `pfc_image.c` gradient tie-break third tier is now covered
  by a crafted-image test; the two `pfc_arith.c` renorm-underflow conditions are recorded in
  `requirements.md` as not unit-testable through the public API, the same treatment given to the
  `pfc_size_mul` CBMC non-convergence.

> ### Follow-up from the 2026-08-24 verification run
>
> **The `MCDC_MIN` ratchet was never moved.** G3.4a's done-when required "total MC/DC ≥ 88%,
> `MCDC_MIN` ratcheted just below", but `flight/Makefile` still carried `MCDC_MIN ?= 73` after
> `8402e5a` added 97 assertions' worth of new coverage. A ratchet left below the measurement is not
> a gate: the coverage those tests bought could regress all the way back to 73% without CI
> objecting. **Open at the time of writing**; being addressed in the 2026-08-24
> verification run, which measures the real figure first rather than assuming 88%.
>
> **`make check` is not the CI suite.** It runs strict/asan/mcdctest/crosscheck/fuzz/stress only.
> The commits from `d8f2ad3` to `b4a6d15` were each verified against `make check`, which leaves
> misra, coverage, mcdc, stackdepth, cbmc, libfuzzer and bigendian unexercised locally. Anyone
> claiming "verified" on a flight change should say *which* gates they ran.

### M4 — `pertype` hygiene (bug-fix scope only, per D1)
**Outcome:** The released product's loose ends are closed or explicitly deferred.

/goal Close or explicitly defer the remaining pertype distribution items.
- **ID:** G4.1
- **Why:** `docs/productization-plan.md` lists aarch64-Linux, Homebrew/Scoop/winget, `cargo
  binstall` and `cibuildwheel` wheels as "remaining (optional, post-first-release)", plus a one-time
  Trusted Publishing config per registry. These are the only open product items.
- **Done when:** Each is either implemented and verified by installing from the new channel, or
  moved to an explicitly-deferred list with a stated reason.
- **Touches:** `docs/productization-plan.md`, `.github/workflows/`
- **Depends on:** none
- **Size:** M
- **Basis:** verified in repo
- **Status:** **Done** (`7558caf`). aarch64-Linux, Homebrew/Scoop/winget, `cargo binstall`,
  `cibuildwheel` wheels and the per-registry Trusted Publishing config are now an explicit
  deferral table in `docs/productization-plan.md`, with reasons, per D1.

> **Dropped (D6):** the former G4.1 branch guard. Merging removes the branch it was guarding
> against, so the guard is no longer the right fix.

---

## Track B — Recommended features

Trimmed per D1 (no new pertype features) and D2 (speculative audience).

### R2 — Ship a self-test binary for flight integrators
**Pitch:** `libpfc`'s evidence lives in `make check`, which needs Python, numpy and a POSIX
toolchain — none present on a target board or in a qualification lab. A freestanding C self-test
running round-trip, bound and containment checks on-target lets an integrator verify it where it
will actually run. Now that the code is public, this is what makes it *usable* rather than just
readable.
**Cost:** Moderate — a curated, host-dependency-free subset of `test_pfc.c`.
**Skip this if:** No third party is expected to integrate it, in which case D2 already says wait.

/goal Add a dependency-free on-target self-test executable for libpfc.
- **ID:** RG2.1
- **Why:** Every existing gate needs a host toolchain the flight target does not have.
- **Done when:** One C entry point builds with only the flight compiler, adds no libc dependency
  beyond what `src/` already uses, runs round-trip + `pfc_bound` + containment checks, returns
  non-zero on failure, and runs under `qemu-ppc` in CI.
- **Touches:** `flight/test/`, `flight/Makefile`, CI workflow
- **Depends on:** G0.1
- **Size:** M
- **Basis:** inferred
- **Status:** **Deferred under D2.** No funded flight opportunity or third-party integrator is
  currently expected; the speculative audience does not justify the engineering cost. Revisit if
  an integrator appears.

### R3 — Guidance for choosing a refresh interval
**Pitch:** Refresh bands are default-off with a cost from +7% to +37% depending on interval, and the
rule found during measurement — pick an interval dividing the band count evenly, since refresh=6
dominates refresh=8 on a 12-band cube — is buried in prose. Now public, it will be read by people
without that context.
**Cost:** Very low (documentation only).
**Skip this if:** The feature stays unused — though then G2.1 should say so.

/goal Document how to choose a SPECTRAL refresh interval for a given cube geometry.
- **ID:** RG3.1
- **Why:** The dominance rule was found empirically and is easy to get wrong.
- **Done when:** `pfc.h`'s `elem` documentation or §2.5.1 states the divides-evenly rule with the
  worked 12-band example and gives the containment bound as a function of the interval.
- **Touches:** `flight/include/pfc.h`, `flight/docs/mission-safety.md`
- **Depends on:** G3.1 (so the numbers quoted are the real-data ones)
- **Size:** S
- **Basis:** verified in repo
- **Status:** **Done.** The `elem` docstring in `flight/include/pfc.h` now states the
  divides-evenly rule with the 12-band example, and `flight/docs/mission-safety.md` §2.5.1 adds
  the explicit containment bound (at most N consecutive bands for interval N). The cost numbers
  remain synthetic until G3.1 is unblocked; the bound itself is structural and data-independent.

### R4 — Independent review of the traceability matrix
**Pitch:** `requirements.md`'s matrix is explicitly solo-authored and §2.1 names independent audit
as the remaining process gap. One external reviewer checking it for honesty and completeness — not
a full IV&V engagement — converts the weakest process claim into a defensible one cheaply. More
valuable now the matrix is public.
**Cost:** Low engineering time; needs a competent person who is not you.
**Skip this if:** No flight opportunity is near enough to justify someone else's time.

/goal Obtain and record an independent review of the requirements traceability matrix.
- **ID:** RG4.1
- **Why:** The matrix concedes solo authorship is not a substitute for IV&V.
- **Done when:** A review record names the reviewer, date, what was checked, and every discrepancy
  found (including "none"); `requirements.md` no longer describes itself as unreviewed.
- **Touches:** `flight/docs/requirements.md`
- **Depends on:** G1.3
- **Size:** S for you; the review is external.
- **Basis:** verified in repo
- **Status:** **Deferred under D2.** No funded flight opportunity is near enough to justify
  asking an external reviewer for their time. `requirements.md` continues to describe the matrix
  as solo-authored and unreviewed. Revisit if a real flight prospect appears.

### R5 — Real-target bring-up on a dev board — SHELVED (D2)
**Pitch:** The largest remaining gap. `bigendian` CI proves big-endian execution under qemu-user,
but §2.4 is explicit that WCET and true target validation need real silicon. Everything else in the
qualification story is now evidenced; this is the one item analysis cannot close.
**Cost:** High — hardware, RTOS, toolchain, plus the WCET work. Months.
**Skip this if:** No funded opportunity — **which is the current position, so this is shelved, not
planned.** Recorded so it isn't mistaken for an oversight.

---

## Risks and unknowns

- **Publishing precedes the truth-fix (D8).** Between M0 and G1.1, the public README asserts an
  error-containment property measured false. Accepted deliberately; the mitigation is to land G1.1
  immediately after the merge. Cheapest de-risk: do G1.1 the same day.
- **The README overstatement may not be the only one.** SPECTRAL's claim sat unverified from
  inception. G1.3 is the read-only pass that finds the rest — worth doing early despite being
  after the merge.
- **§2.5.1's cost figures are synthetic and now public.** The same measurement swung an order of
  magnitude on different test data. G3.1 is the fix and is scheduled early per D5.
- **The merge conflict is understood but must be resolved correctly.** Taking flight-core's side of
  `docs/data-type-opportunities.md` would silently drop the OTLP entry. Take master's side.
- **Post-merge, flight work pushes to public GitHub by default.** The old gitea-only reflex is now
  wrong; G0.2 exists to make that explicit before muscle memory causes a surprise.

## Questions for you

- **Does `ROADMAP.md` become a tracked file after the merge?** **Answered by action** — it was
  committed in `5532fc6` and is tracked on `master`.
- **No horizon or constraint was specified** (the optional switches block was left intact). I have
  assumed no deadline and solo working. **Still open**, but far less consequential now Track A is
  complete: it would only affect the pace of any future work, not the current plan.
- **Should the three open `TODO.md` items be reopened?** `split` transform, 2D predictors / RLE for
  zero-runs, and longer per-track-adaptive audio filter orders are all *features*, so D1 rules them
  out. They are the only remaining `pertype` work of any kind. Reopening them means revisiting D1.
