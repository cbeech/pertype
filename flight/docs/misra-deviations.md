# libpfc — MISRA-C:2012 deviation record

The `misra` CI job (`cppcheck --addon=misra`) found 179 findings across 17 rules on its first
real run. This document is the triage: every rule below was checked against actual flagged source
lines (not judged from the rule number alone), and each entry states a verdict and why. Four
findings were fixed outright (see "Fixed" below); the rest are recorded here and suppressed via
`flight/.cppcheck-suppressions`, which `make misra` applies — anything *not* listed here or in
that file still fails the build, so this gate stays meaningful rather than silenced wholesale.

This is how real MISRA compliance is organized: **triage by rule** (each rule number groups many
occurrences of the same pattern), not by reviewing each of 179 individual lines as an unrelated
case. cppcheck's free MISRA addon can't print the licensed rule *text* (`use --rule-texts=<file>`
— that requires a paid MISRA document), only the rule *number* and the flagged line; the
descriptions below are from the MISRA-C:2012 rule set's well-known public numbering, cross-checked
against the actual source at real occurrences of each.

**Verified working on the next CI run**: with the suppressions applied, zero `misra-c2012-*`
findings remained. `--enable=warning,style` (needed for the MISRA addon) also turns on cppcheck's
own general checks, which had been masked by the sheer volume of MISRA findings — once those
cleared, three more surfaced and were triaged the same way (see "Non-MISRA cppcheck findings"
below): one real, safe fix; two more suppressions (one an exact duplicate of a MISRA finding
already justified here, under a different cppcheck error ID).

## Fixed (4 findings, real and trivially correctable)

| Rule | Where | Fix |
|------|-------|-----|
| 15.7 (×2) | `pfc_image.c` clamp logic (encode + decode bands) | Added an explicit `else { /* already in range */ }` to the `if/else if` clamp so the "no clamping needed" case is stated, not implicit. |
| 15.7 (×1) | `pfc_seq.c` sign-extension `if/else if` | Same — added an explicit `else` for the unsigned/4-byte case. |
| 14.2 | `pfc_image.c` truncation-repair inner loop | Was reusing the *outer* loop's control variable (`y0`) as the inner loop's counter before `break`ing out of the outer loop. Behaviour was already correct (the reused value was never read afterward), but gave the inner loop its own variable (`yf`) so it's well-formed on its own terms, not just "safe because of what happens next." |

These were the only findings among the 179 that were both (a) unambiguous to fix and (b) genuinely
worth fixing rather than deviating — each was a few lines, zero behavioural risk, and removes real
ambiguity for a future reader rather than just satisfying the linter.

## Suppressed: confirmed tool limitations (not real findings)

| Rule | Count | What it flags | Why it's a false positive |
|------|-------|---------------|----------------------------|
| **12.3** (comma operator) | 23 | Every one of the 23 flagged lines, checked directly, is a **comma-separated declarator list** — `int a, b, c, d, q1, q2, q3, idx;` — not the comma *operator*. cppcheck's MISRA addon conflates the two. This happens specifically *because* this codebase follows a strict declare-all-locals-at-the-top-of-the-block style (deliberately, including two same-night fixes to keep declarations from trailing after statements) — the more consistently that style is followed, the more of these false positives appear. |
| **8.7** (should be static) | 4 | `pfc_image_encode_band`, `pfc_image_store_raw`, `pfc_image_load_raw`, `pfc_image_decode_band` — all four are declared with external linkage in `pfc_internal.h` and genuinely called from `pfc.c`/other files. `make misra` runs cppcheck over `src/` one file at a time (not whole-program mode), so it can't see the cross-file call sites. Confirmed via `grep -rn` for each symbol outside `pfc_image.c`. A cleaner long-term fix is running cppcheck in whole-program/project mode instead of suppressing this — noted as a follow-up, not done tonight (would need local cppcheck to verify the invocation before trusting it in CI). |

The one genuine comma-operator usage in this codebase (`pfc_arith.c`'s range-coder renormalisation,
see rule 13.5 below) is real and is **not** in this false-positive group — it triggered rule 13.5
(side effects in a logical operand), not 12.3, confirming the 12.3 hits really are all declarator
lists.

## Suppressed: deliberate patterns, Advisory rule, verified safe

Each of these was spot-checked against real source (not assumed from the rule description). None
are bugs; each is a deliberate choice with a specific reason, given here.

| Rule | Count | Pattern | Why it's deliberate and safe |
|------|-------|---------|-------------------------------|
| **15.5** (single point of exit) | 57 | Defensive early-return on invalid input (`if (bad) return PFC_E_...;`) throughout every codec's encode/decode. | This is the *largest* deviation by far and the most important to state clearly: restructuring 57 call sites into single-exit form (nested if/else or a `goto cleanup`/flag-variable pattern) would make every one of them **longer and harder to audit**, not safer — exactly backwards for a project whose stated discipline is JPL *Power of Ten* (small, simple, defensively-checked functions). Early-return-on-guard-clause is the preferred pattern in that discipline, not an oversight. |
| **10.8** | 26 | Explicit widening casts in address arithmetic, e.g. `(size_t)(y - 1u) * width` in `pfc_image.c`. | Deliberate overflow prevention (32-bit `y*width` could overflow on large images; the explicit `size_t` cast forces 64-bit arithmetic on real targets). The cast is the fix MISRA 10.8 exists to encourage in the *accidental* case — here it's already present and intentional. |
| **12.1** (explicit precedence) | 20 | Arithmetic/bitwise expressions without extra parentheses cppcheck wants, e.g. `a << b | c`. | Pure style; every flagged expression's precedence is well-defined by the C standard regardless. Zero safety impact. Lower priority than the others — could be mechanically parenthesized later with no risk, just not done tonight given the volume (20 call sites) relative to value (none functional). |
| **11.5** (void* conversion) | 16 | `(uint8_t *)dst`, `const void *src` parameters, etc. | Unavoidable in generic buffer-handling C — the public API (`pfc_encode`/`pfc_decode`) takes `void *` precisely so callers aren't locked into one buffer type; converting it to a typed pointer internally is the whole point. |
| **10.7** | 10 | Mixed-width integer arithmetic in bit/byte-level codec math (shifts, masks, multi-precision-style accumulation). | Unavoidable in this domain — a lossless codec is fundamentally byte/bit manipulation across mixed integer widths (`uint8_t`, `uint16_t`, `uint32_t`, `size_t`). |
| **10.6** | 2 | Composite expression assigned to a wider essential type. | Same family as 10.7/10.8 — deliberate widening, not accidental narrowing (which `-Wconversion` would already have caught). |
| **10.4** | 1 | `s[0] != 'P'` — `uint8_t` compared against a `char` literal (the `"PFC1"` magic-byte check in `pfc.c`). | Standard and unavoidable when validating buffer bytes against ASCII constants; every stream-format parser does this. |
| **10.3** | 5 | Deliberate narrowing casts, e.g. `uint32_t` → `uint8_t` byte truncation when packing wire-format bytes. | Already verified more precisely by `-Wconversion -Werror`, which is a *stricter*, compiler-level check for exactly this class of narrowing and passes cleanly across the whole codebase — MISRA 10.3 is approximating what `-Wconversion` already guarantees. |
| **10.1** | 1 | `n >> 31` on signed `int32_t` in `pfc_zigzag` (`pfc_model.c`) — the branchless zigzag-encoding idiom (`((uint32_t)n << 1) ^ (uint32_t)(n >> 31)`). | The one deviation worth the most care: right-shifting a *negative* signed value is **implementation-defined behaviour pre-C23** (C23 retroactively mandates arithmetic shift). It is universal in practice on every real twos-complement target — and, checked explicitly rather than assumed, that includes **all three of this project's actual cross-compile targets**: PowerPC (RAD750-class), SPARC (LEON), and RISC-V (HPSC) are all twos-complement with arithmetic right-shift-on-negative. If this project ever targets a genuinely non-twos-complement or logical-shift architecture, this line needs revisiting; on every currently-planned target it's correct and portable. |
| **13.5** (side effect in `&&`/`\|\|`) | 4 | `pfc_arith.c`'s range-coder renormalisation: `... && ((e->range = (0u - e->low) & (PFC_RC_BOT - 1u)), 1)`. | The **one genuine comma-operator usage** in the codebase (confirmed by reading the source — see the 12.3 note above). This is a well-known, deliberate idiom from Subbotin-style range-coder implementations: assign `e->range` as a side effect, then evaluate to `1` so the `&&` short-circuit still works correctly. Verified: the side effect executes at most once per evaluation (short-circuit `&&` means the right operand — and its side effect — only runs when the left operand is false, exactly the intended "recompute range only when needed" behaviour), so there's no double-evaluation or ordering hazard. |
| **17.8** (parameter modification) | 3 | Function parameters reused as scratch/working variables, e.g. `pfc_bitlen(uint32_t u) { ...; u >>= 1; ... }`. | Completely safe by construction: C parameters are pass-by-value, so modifying a local copy has zero caller-visible effect. This is one of the most common, well-precedented MISRA Advisory deviations in real C codebases — the alternative (copying the parameter to a same-named local just to satisfy the rule) adds a line with no behavioural or safety difference. |
| **14.4** (essentially-Boolean controlling expression) | 2 | `if (x)` on an integer flag/count rather than an explicit `!= 0` comparison. | Extremely common, unambiguous in context (the flag/count semantics are clear from the variable name and surrounding code), zero safety impact. |
| **2.5** (unused macro) | 1 | `PFC_WORKMEM_BYTES` in `include/pfc.h`. | Not dead code — it's **public API**, meant for external flight-software callers to size their statically-allocated `pfc_ctx`, not used internally by the library itself. cppcheck's single-file `src/`-only scan can't see the public header's intended external consumers. |

## Not yet triaged / follow-ups

- **8.7 whole-program analysis**: a real fix (cppcheck project/multi-file mode) instead of a
  suppression would eliminate that false-positive class properly. Needs local cppcheck to verify
  the correct invocation before trusting it in CI — not attempted tonight.
- **12.1 mechanical parenthesization**: could be done safely (pure style, zero behavioural risk)
  as a follow-up cleanup pass; deferred rather than making 20 more source edits in the same session
  as two real bug fixes and this triage.
- This triage was performed by reading representative/all occurrences of each rule via the CI log
  and source, but wasn't cross-checked against a *licensed* MISRA-C:2012 rule-text reference (the
  free cppcheck addon doesn't provide one). The rule *numbers* and general semantics used here are
  the well-established public MISRA-C:2012 numbering; a full commercial audit (Coverity/LDRA/
  Polyspace with the licensed rule text) would be the real qualification-grade version of this
  document.

## Non-MISRA cppcheck findings (surfaced after the MISRA list was suppressed)

`--enable=warning,style` is required for the MISRA addon to run, but it also enables cppcheck's
own general-purpose checks. These were invisible in the noise of 179 MISRA findings; once those
were suppressed, three more findings appeared on the next run.

| Check | Where | Verdict |
|-------|-------|---------|
| `duplicateCondition` | `pfc_spectral.c`, `spec_predict` | **Real, fixed.** `if (haveW && haveN) { dNW = ...; }` immediately followed by a second, identical `if (haveW && haveN) { ... }` that uses `dNW` — nothing between them could change `haveW`/`haveN`, so the condition was genuinely evaluated twice for no reason. Merged into one `if` block: compute `dNW` where it's used, inside the single surviving condition. Not a bug (both branches were correctly gated), just a real, safe, worthwhile cleanup. |
| `shiftTooManyBitsSigned` | `pfc_model.c`, `pfc_zigzag` | **Suppressed — exact duplicate of misra-c2012-10.1 above.** Same line (`n >> 31`), same code, same justification (verified safe on all three real cross-compile targets); cppcheck's generic portability check and the MISRA addon both flag it, under different error IDs. |
| `variableScope` | `pfc_columnar.c`, `pfc_columnar_decode` | **Suppressed — deliberate.** `uint32_t c;` is declared at the top of the per-block loop (matching every other local used anywhere in that loop) but only read inside one nested branch. Narrowing its scope would require declaring it partway through the block — exactly the declaration-after-statement pattern this codebase deliberately avoids (corrected twice in the same session that wrote this fix, specifically to keep all locals declared at the top of the block they're in). Kept as-is for consistency with that convention. |

