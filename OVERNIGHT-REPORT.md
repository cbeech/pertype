# Overnight run — post-merge verification slate (2026-08-24)

Branch: `master` (single branch since the 2026-08-19 merge). Push target: **`origin` (gitea) only,
never `github`** — see the authorization note below.

The previous run's report was archived to `OVERNIGHT-REPORT-2026-07-28.md`.

**User-authorized deviation from the `overnight` skill's "never push" hard rule:** at kickoff the
user chose "run it, and push to gitea as I go" over the offered no-push default. Every push is
gated on the relevant `make` target passing first. **`github` receives nothing this run**, so
`master` on public GitHub will end the night behind `origin` by design — that push is deliberately
left as a morning decision for the user.

## Why this queue

The survey found something worth stating plainly: **`ROADMAP.md`'s Track A is already complete.**
Every goal — G0.1, G0.2, G1.1, G1.2, G1.3, G2.1, G3.1, G3.2, G3.3, G3.4a–d, G4.1 and Track B R3 —
landed as a commit between `d8f2ad3` and `b4a6d15`. The document marks only two of them done, so it
reads as though nearly nothing has happened. Both remotes were in sync and the tree was clean.

So there is no feature backlog to grind: the three open `TODO.md` items are all *features*, which
decision D1 explicitly puts out of scope. What is genuinely open is **verification and gate
hygiene**, and the survey turned up two concrete gaps before any work started:

1. `flight/Makefile:152` still reads `MCDC_MIN ?= 73`, but G3.4a's own done-when required
   "total MC/DC ≥ 88%, `MCDC_MIN` ratcheted just below". The ratchet was never moved, so the gate
   no longer protects the work `8402e5a` just did.
2. The last five commits claim only `make check` was verified. `make check` runs
   strict/asan/mcdctest/crosscheck/fuzz/stress — it does **not** run misra, coverage, mcdc,
   stackdepth, cbmc, libfuzzer or bigendian. Those seven gates had not been exercised locally
   since the merge.

## Queue

| # | Task | Status |
|---|------|--------|
| 1 | Reproduce all 8 CI gates under Docker on merged `master` | running |
| 1b | Fix `test_crosscheck.py` swallowing real failures as "skip" | queued |
| 2 | Ratchet `MCDC_MIN` (and any other loose gate) to the measured value | queued |
| 3 | Sync `ROADMAP.md` statuses with the commits that closed them | queued |
| 4 | `pertype` regression run — pytest + `cargo test` | queued |
| 5 | Extended libFuzzer soak, far past CI's 300 s budget | queued |
| 6 | Hygiene — archive the old report, remove the stray `flight;C` dir | in progress |

## Needs you (not queued)

- **The `github` push.** Nothing reached public GitHub this run, by the kickoff decision.
- **RG4.1** (independent review of the traceability matrix) and **R5** (real-target bring-up) —
  both need a person or funding, not machine time. Deferred under D2.
- **The three open `TODO.md` items** — `split` transform, 2D predictors/RLE, longer audio filter
  orders. All features, so all blocked by D1. Reopening them is a user decision.
- **`ROADMAP.md`'s unanswered horizon question** — no deadline or constraint was ever specified.

---

## Results

