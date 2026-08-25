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

### Task 3 — Sync `ROADMAP.md` statuses ✅ done (`03f58f9`, pushed to gitea)

Eleven completed goals were reading as open work. Annotated G0.1, G0.2, G1.1, G1.2, G1.3, G2.1,
G3.2, G3.3, G3.4a–d and G4.1 with a Status line naming the commit that closed each, and rewrote the
header (one branch now, `libpfc` public in `flight/`, `flight-core` retired).

**Verified rather than assumed:** `git ls-remote --heads` on both remotes confirms `flight-core` is
genuinely gone, and that `github` carries `master` **only** — `research/llm-vram` exists on `origin`
alone, so the D2/G0.2 push-policy invariant still holds after the merge.

Answered the roadmap's own open question about tracking (`5532fc6` settled it by action), and
recorded the three open `TODO.md` items as blocked by D1 — worth stating because they are the only
remaining `pertype` work of any kind, so "pertype is done" is true only while D1 stands.

Two follow-ups recorded in the document as open, not fixed here:

- **`MCDC_MIN` was never ratcheted** — see task 2.
- **`make check` is not the CI suite.** Every commit from `d8f2ad3` to `b4a6d15` was verified
  against `make check`, which does not run misra, coverage, mcdc, stackdepth, cbmc, libfuzzer or
  bigendian. That gap is exactly what task 1 exists to close.

### Task 4 — `pertype` regression run ✅ done (`6e4b532`, pushed to gitea) — one real bug found

`python -m pytest tests/ -q` on Python 3.12: **136 passed, 1 skipped, 12 errors.**

The 12 errors were all `tests/test_rust_port.py`, dying in `ctypes.CDLL` with
`[WinError 193] %1 is not a valid Win32 application`. That is a genuine test bug rather than an
environment quirk, and the file's own docstring is what makes it so: it promises the module is
*"skipped unless the cdylib is built"*, but the guard globbed for `libpertype.so` on **every**
platform. A Linux ELF left in `rust/target/release/` by a build on the Linux box therefore
satisfied the skip guard on Windows, and the module then tried to load it.

Fixed by resolving the cdylib name per platform (`pertype.dll` / `libpertype.dylib` /
`libpertype.so`), so the guard reflects what cargo actually emits here. **Verified: 12 errors → 12
clean skips.** In D1's bug-fix scope — the guard did not do what it said, and a test that errors on
an unsupported platform is noise that trains you to ignore red.

`cargo test` was not run: no Windows cdylib exists to test against, and building one would be new
platform work rather than a bug fix. Noted below under what a Linux run should pick up.

### Task 1b — `test_crosscheck.py` reported real failures as "skip" ✅ fixed (`2bd179f`, pushed)

Found by reading the harness before running it, not by a failing test — which is the point: this
defect's whole nature is that it cannot produce a failing test.

The real-AVIRIS R7 case wrapped **both** the data acquisition and the two `check()` calls in a bare
`except Exception`, whose handler prints `(skip real AVIRIS spectral cross-check: ...)`. Since
`c_encode()` asserts `st == 0` on the C encoder's return, a genuine encode failure on real
hyperspectral data was caught by that handler and downgraded to a skip.

Fixed by narrowing the `try` to the import and the load — the only genuinely skippable steps — and
moving the `check()` calls out of it. The CyCIF block directly below already uses that shape
(`if tifs:`), so the two are now consistent.

**Verified by mutation** rather than by assertion. Injecting `count=0` into the SPECTRAL params
makes the parameter guard reject, firing the assert:

| variant | encoder | what it printed | exit |
|---|---|---|---|
| pre-fix | broken | `(skip …: C encode status 1)` then `8 passed, 0 failed` | **0** |
| post-fix | broken | `AssertionError: C encode status 1` | **1** |
| post-fix | healthy | `10 passed, 0 failed` | 0 |

The pre-fix run did not merely hide the failure — it **claimed success while silently dropping two
checks from its own total**, 8 where a healthy run reports 10. Nothing downstream would have
noticed.

**Adjacent finding, deliberately not changed:** the CyCIF block at `test_crosscheck.py:130` points
at `/tmp/claude-1000/-home-craig-Dev-compression/<session-uuid>/scratchpad/spatialomics`, a
scratchpad path belonging to one long-finished session. It can never match, so that case is dead
code that silently never runs. Left alone because making it live could start failing on the Linux
box in ways I cannot verify from here (`import tifffile` sits inside the `if tifs:` block, ungated),
and that is a judgement call rather than a bug fix. Worth a look when someone is at that machine.

### Task 1 — Reproduce all 9 CI gates on merged `master` ⚠️ 8 pass, 1 fail

Run in a clean `debian:bookworm` container against the merged tree, with `build/` and `src/*.o`
deleted after the copy (the stale-artifact hazard that produced a false pass in the July run —
verified absent this time, the log records `build/` missing before the first compile).

| gate | result | evidence |
|---|---|---|
| `native` (`make check`) | ✅ PASS | 162 unit + 97 MC/DC-targeted + 10 cross-check + 20 000 fuzz + 15 063 stress, **0 failures** |
| `misra` | ✅ PASS | cppcheck 2.10, MISRA-C:2012, 9/9 files clean |
| `coverage` | ✅ PASS | **98.3% line, 89.3% branch, 100% function** — matches the documented 98.4/89.2 baseline |
| `mcdc` | ✅ PASS | **80.87%** (93/115) against a floor of 73 — see the finding below |
| `stackdepth` (host) | ✅ PASS | 632 B worst case vs 1024 B budget |
| `stackdepth-ppc` (flight ABI) | ✅ PASS | 464 B worst case |
| `libfuzzer` | ❌ **FAIL rc=139** | SIGSEGV, no fuzzer output at all — see task 1c |
| `bigendian` | ✅ PASS | 162 tests on emulated BE PowerPC; **R4 verified**: BE and LE encoders byte-identical |
| `cbmc` | ✅ PASS | `** 0 of 152 failed`, VERIFICATION SUCCESSFUL under the 32-bit model |

Two things worth noting beyond the pass/fail column. The real-AVIRIS cross-check **ran** here
(scipy present, `.tmp_aviris/` copied in) — 10/10 including `spectral-aviris-refresh4` — so R7
covers the containment feature on real instrument data in a clean environment, not just on this
workstation. And `test_pfc` printed workmem **330 708 B**, independently confirming the figure G1.2
corrected the README to.

### 🔴 Finding — the MC/DC work did not reach its own target, and the loose gate hid it

`make mcdc` measures **80.87%** (93 of 115 conditions). G3.4a's done-when required **"total MC/DC
≥ 88%, `MCDC_MIN` ratcheted just below"**. The pre-G3.4 baseline was 76.52% (88/115), so:

- `8402e5a` claimed to cover **17** conditions — 14 class-A parameter/header guards (G3.4a), 2
  class-C store-raw (G3.4c) and 1 class-D gradient tie-break (G3.4d).
- The measurement moved coverage by **5** conditions (88 → 93).

Twelve conditions' worth of intended coverage did not materialise. The likely mechanism is the
distinction MC/DC exists to enforce: the new tests reach the *decisions*, but do not supply each
condition's **independence pair** — the vector where that condition alone flips the outcome. That
is precisely the gap branch coverage cannot see, which is why `coverage` stayed green at 89.3%
throughout.

**The un-ratcheted floor is why this went unnoticed.** Had `MCDC_MIN` been raised to ~88 as G3.4a
required, `make mcdc` would have failed immediately and shown the shortfall. Left at 73, it passed
comfortably and reported PASS. A ratchet that is not ratcheted does not merely fail to protect new
work — it actively conceals whether that work happened.

Consequence for the roadmap: my earlier "Done" annotations for G3.4a/c/d are too generous.
Correcting them, with the per-condition evidence, is under way.

### Task 1c — `libfuzzer` rc=139 ✅ investigated, **not a libpfc defect**

Isolated in three steps rather than guessed at.

1. **Control:** a trivial libFuzzer target with no libpfc involvement built and ran fine
   (`--- control rc=0 ---`), so the toolchain and ASan runtime work in this container.
2. **Subject, short run:** the real `fuzz_pfc` harness at `-runs=200` ran fine too.
3. **Subject, exact gate invocation** (`-max_total_time=300 -max_len=65536`), run **alone**:
   **rc=0, 312 714 executions, corpus 0 → 161, coverage 471 edges / 2 232 features, no ASan or
   UBSan report, no crash artifact.**

So the crash does not reproduce on demand. **My first attribution here was wrong and is corrected
below** — I guessed resource contention from a second container. The soak (task 5) then hit the
identical failure while running completely alone, which killed that theory, and the follow-up
experiment identified the real cause. See "Root cause" at the end of task 5.

**A flaw in my own harness, worth recording:** the gate script piped the fuzzer's stdout into
`tail -20`. libFuzzer block-buffers stdout to a pipe, so when the process died the entire buffer —
including any ASan report — was lost, which is why the gate log showed a bare `rc=139` with no
output at all. **Never diagnose a crashing process through a pipe.** The reproduction wrote
straight to a file, which is why it would have captured a real report had one existed.

### Task 2 — Ratchet `MCDC_MIN` ✅ done (`5474bf3`, pushed to gitea)

Raised 73 → **80** against the measured 80.87%, with the reasoning recorded in the Makefile.

**Verified the gate both passes and bites** — checking only that it passes would not distinguish a
working floor from a broken one:

| floor | exit | meaning |
|---|---|---|
| 80 | 0 | passes at the new floor |
| 82 | 1 | the floor genuinely fails a build below it |
| 88 | 1 | G3.4a's own done-when target is **not** met |

(My first attempt at this check was wrong — I read `$?` after piping to `tail`, so I was reading
`tail`'s exit code, not the gate's. Re-run without the pipe.)

`ROADMAP.md` now reopens G3.4a (partial), G3.4c (not achieved) and G3.4d (half done) with the
per-condition evidence, and re-confirms G3.4b as genuinely complete — its 8 conditions are
*supposed* to stay uncovered, since that goal asked for classification, not coverage.

### Task 7 — Why the MC/DC tests didn't land, and a defect found while working it out

Rather than just re-marking G3.4a/c/d as open, I traced *why* `8402e5a`'s tests moved 5 conditions
instead of 17. The answer is the same for most of them and it is structural, not a matter of
writing more test cases.

**The codec-level parameter guards are unreachable through the public API.** `flight/src/pfc.c:68`
already rejects `width == 0 || width > PFC_BLOCK_BYTES` before dispatching to COLUMNAR, so
`pfc_columnar.c:25`'s C1/C2 can never be exercised by a call to `pfc_encode()`. `8402e5a`'s tests
go in the front door, get rejected upstream, and never reach the guard they were written to cover.
The same shape applies to the SPECTRAL and IMAGE header guards. Covering these requires calling the
internal entry points (`pfc_columnar_encode` et al., declared in `pfc_internal.h`) **directly**,
bypassing the front-door validation — a different technique from the one the goal assumed.

That makes G3.4a's "14 class-A conditions" an overestimate of what is reachable, in the same way
G3.4b's 8 turned out to be. The honest split is narrower than the roadmap's.

#### 🔴 Defect — `pfc_seq_encode` divides by `p->elem` before validating it

Found by asking why one specific condition could never be covered.

```c
uint32_t block = PFC_BLOCK_BYTES / p->elem;   /* pfc_seq.c:69 — divide FIRST */
size_t at;
size_t i0;

if ((p->count == 0u) || (block == 0u)) {      /* pfc_seq.c:73 — guard AFTER */
    return PFC_E_PARAM;
}
```

`pfc_columnar.c` does the identical job in the correct order — guard on `rw`, *then* compute
`PFC_BLOCK_BYTES / rw`. SEQ is the odd one out, and there are two consequences:

1. **`elem == 0` is a division by zero** — undefined behaviour, SIGFPE on x86 — reached before any
   validation runs.
2. **`block == 0u` is dead code.** `PFC_BLOCK_BYTES` is 65536 and `p->elem` is a `uint8_t`, so
   `65536 / elem` spans 257…65536 and is never 0. This is exactly why `pfc_seq.c:73` C2 has never
   been coverable — and why chasing it with more tests was never going to work.

**Reachability:** not live through `pfc_encode()`, which validates `elem ∈ {1,2,4}` at
`pfc.c:48` first. It is reachable through the internal entry point, which is non-`static` and
declared in `pfc_internal.h`. So: latent, not exploitable from the public API — but in a codebase
whose whole claim is the absence of undefined behaviour, a guard that divides before it checks is
the wrong shape, and neither the CBMC proof nor the MISRA gate flagged it.

Worth noting what *did* find it: the MC/DC metric. An uncoverable condition was the symptom; the
defect was the cause. That is precisely the argument for the metric that D3 chose to invest in.

#### Fix, and what it bought — ✅ done (`c49ea41`, pushed to gitea)

`pfc_seq_encode`'s guard is now `(p->elem == 0u) || (p->count == 0u)` with the division moved
below it, matching `pfc_columnar_encode`'s ordering. Both conditions are reachable and covered;
the dead `block == 0u` condition is gone. The decode side's identical-looking check is deliberately
left alone — there `block` is read from the untrusted stream, so it is genuinely live.

I also addressed the structural cause rather than only the symptom: `mcdc_internal_guards()` calls
the codec entry points **directly**, which is the only way to reach guards the dispatcher shadows.

**Verified in a clean container, all four checks:**

| check | result |
|---|---|
| UBSan, direct call with `elem=0` | `returned st=1` (`PFC_E_PARAM`), **no runtime error** — was `division by zero` |
| `make check` | 162 + **110** (was 97) + 10 + 15 063, `ALL CHECKS PASSED` |
| `make misra` | clean |
| `make mcdc` | PASS |

**MC/DC 80.87% → 83.48%** (93 → 96 of 115). Per-file delta: `pfc_columnar.c` 7/11 → 9/11 (the two
direct-call guards), `pfc_seq.c` 12/15 → 13/15 (the now-reachable `elem == 0`). `MCDC_MIN` ratcheted
80 → 83, verified to pass at 83 and **fail at 84**.

#### G3.4c reclassified — unreachable in practice, not merely uncovered

My new store-raw tests asserted the RAW flag and passed, yet the conditions stayed uncovered. That
apparent contradiction has a clean explanation, and it is worth more than another test would have
been: each codec calls `pfc_rc_enc_init(&e, w->scratch, raw_bytes)`, so the coder's capacity **is**
the raw size, and `pfc_rc_put` sets `overflow` the moment it cannot write. Incompressible data
therefore trips `e.overflow` on the way past the limit rather than arriving at
`e.pos >= raw_bytes` with overflow still clear. That pairing needs the coded output to land on
*exactly* `raw_bytes` — structurally possible, a measure-zero coincidence in practice.

So G3.4c is closed as unreachable-in-practice (documented beside the G3.4b guards), not left open
implying more tests would help. The tests are kept: unlike the pre-existing ones, they assert the
**RAW flag in the block header**, proving the fallback genuinely fires rather than inferring it
from `PFC_OK` and a round-trip — which hold whether or not it fired.

**Still open in G3.4a:** `pfc_spectral.c:199` C3–C6 and `pfc_image.c:309` C3/C4, both needing the
same direct-call treatment that worked for COLUMNAR. `pfc_image.c:121` C4 (G3.4d) also remains
uncovered despite a test written for it.

**Two mistakes of mine in this task, both caught before they mattered:**
- The generated C had a `\n` escape collapse into a real newline, breaking the build. Caught by
  `make check`, fixed, re-verified — the reason the fix was not committed until the suite was green.
- My first floor check read `$?` after a pipe, so it was reading `tail`'s exit code rather than the
  gate's. Re-run without the pipe; the "must fail at 84" half is what makes it a real check.

### Task 6 — Hygiene ✅ done (`2674790`, pushed to gitea)

- Archived the previous report to `OVERNIGHT-REPORT-2026-07-28.md` and opened this one, following
  the existing rotation pattern.
- Removed the stray `flight;C` directory at the repo root — an empty artifact of a shell command
  where `flight;C:\...` got parsed as two words. Untracked and empty, so nothing was at risk, but
  it staged through `Recycle/2026-08-24-stray-dir-flight-semicolon-C` rather than being deleted
  outright, per the unattended-deletion rule. **It is still there** — delete it whenever you like.
- Added `/Recycle/` to `.gitignore`.

`.tmp_aviris/` (5.7 MB) was **kept deliberately**: `test_crosscheck.py` resolves the real-AVIRIS
scene from it, and dropping it would silently downgrade the R7 cross-check to a skip on this
machine.

### Task 5 — Extended libFuzzer soak ⏳ running

Two hours, four parallel workers, seeded from valid streams (`emit.c` fixtures split on their
length prefixes) with the corpus persisted to the scratchpad so future runs compound.

This is not "CI but longer". `mission-safety.md` §2.6 formally accepts that each CI run fuzzes a
fresh 300 s **from an empty corpus**, because corpus persistence could not be made to work on the
Gitea instance — so coverage never compounds there. The soak does the thing CI structurally cannot.
It is also running **alone**, which doubles as a cleaner read on the task 1c contention theory.

### Task 5 — Extended libFuzzer soak ✅ complete, **0 crashes** — and it caught the rc=139 cause

Two hours, four workers, seeded from valid `emit.c` fixture streams, corpus persisted.

| metric | result |
|---|---|
| executions | **~630 000** across 3 completed workers (~155 k each) |
| corpus | 3 seeds → **3 305 files** |
| coverage reached | 759 edges, 4 163 features |
| **crashes / leaks / OOM / timeouts** | **0** — no artifacts written |
| peak RSS | 417 MB |

That is roughly **2 100× the execution volume of one CI run** on a corpus that compounded rather
than restarting from empty, and it found nothing. Good positive evidence for R6.

But the soak exited **rc=1**, and the reason matters more than the clean fuzzing result.

#### 🔴 Root cause of `rc=139` — ASan is unreliable on this machine, ~25% of the time

`fuzz-1.log` contained exactly one line: `Segmentation fault`. No libFuzzer banner, no ASan report.
One of four workers died at startup — **while the soak was the only container running**, which
disproves the contention theory I offered in task 1c.

The signature (intermittent, at startup, before any output, bare SIGSEGV) fits an ASan shadow-
mapping failure under high ASLR entropy. `vm.mmap_rnd_bits` is **32** on this WSL2 kernel
(6.18.33.2-microsoft-standard-WSL2). Tested directly — 100 startups each:

| binary | startup failures |
|---|---|
| `fuzz_pfc` harness | **30 / 100** |
| **control** — trivial libFuzzer target, *zero* libpfc code | **23 / 100** |
| `fuzz_pfc` with `ASAN_OPTIONS=detect_leaks=0` | 38 / 100 |

**The control settles it.** A target containing no libpfc code at all fails at a comparable rate,
so this is the toolchain/kernel combination, not the codec. `rc=139` was never evidence of a
decoder bug.

**This has a consequence beyond the fuzzer, and it is the part worth acting on:** roughly a quarter
of *every* ASan process on this machine dies at startup at random. `make check` runs an ASan build,
so **any local verification here can fail spuriously ~25% of the time** — and it will look like a
real failure, because the process dies before it can say otherwise. It passed every time tonight,
which is luck rather than evidence. If a local ASan run fails with a bare segfault and no report,
re-run it before believing it.

Likely unaffected on the real Gitea runner, which is a different kernel — but that is an
assumption, not something I verified.

**Mitigations, none applied** (all need privileges this container doesn't have, and none is a
source change): `sysctl -w vm.mmap_rnd_bits=28` on the WSL2 host, `docker run --privileged` with
`setarch -R` (plain `setarch -R` failed here: `Operation not permitted`), or `--cap-add=SYS_PTRACE`.
The host-level `mmap_rnd_bits` change is the usual fix and would apply to all local ASan work.

---

## Summary

**Queue: 8 of 8 complete.** Nothing parked, nothing skipped.

| # | task | outcome |
|---|---|---|
| 1 | Reproduce all 9 CI gates | 8 pass; the 1 failure root-caused to the environment |
| 1b | `test_crosscheck.py` swallowing failures | fixed, mutation-verified (`2bd179f`) |
| 1c | `libfuzzer` rc=139 | root-caused: ASan/ASLR, **not** libpfc |
| 2 | Ratchet `MCDC_MIN` | 73 → 83 (`5474bf3`, `c49ea41`) |
| 3 | Sync `ROADMAP.md` | 11 goals annotated, 3 correctly reopened (`03f58f9`) |
| 4 | `pertype` regression | 136 pass; 1 real test bug fixed (`6e4b532`) |
| 5 | Fuzz soak | ~630 k executions, 0 crashes |
| 6 | Hygiene | report rotated, stray dir staged (`2674790`) |
| 7 | MC/DC root cause + fix | real defect found and fixed (`c49ea41`) |

### Three real defects, none of which a failing test would have revealed

1. **`pfc_seq_encode` divided by `p->elem` before validating it** — UBSan-confirmed division by
   zero; its `block == 0u` guard was dead code. Found by asking why an MC/DC condition was
   *uncoverable*. The uncoverable condition was the symptom; the defect was the cause.
2. **`test_crosscheck.py` could not fail** — a bare `except Exception` turned genuine encode
   failures into a "skip". Pre-fix it reported *"8 passed, 0 failed"*, **exit 0**, over a
   deliberately broken encoder.
3. **The MC/DC ratchet was never ratcheted** — which is what hid that 5 of a claimed 17 conditions
   were actually covered. At a floor of 73 the gate reported PASS throughout.

The common thread is that all three are failures of *evidence*, not of code behaviour: a test that
can't fail, a gate that can't bite, and a guard that checks the wrong thing. None would ever show
up as a red build.

### Where I was wrong

- **Attributed `rc=139` to container contention.** The soak hit the identical failure running
  alone, disproving it. The corrected cause is ASan + `mmap_rnd_bits=32`, established with a
  no-project-code control. Task 1c is amended in place rather than quietly rewritten.
- **Marked eleven roadmap goals "Done" from commit messages** at the start of the run, then had to
  reopen three once measured. Commit messages are claims, not evidence.
- **Read `$?` after a pipe** when checking the MC/DC floor, so my first "verification" measured
  `tail`'s exit code.
- **Generated C with a `\n` that collapsed into a real newline**, breaking the build — caught by
  `make check` before commit.

### Needs you

1. **The `github` push.** Commits `03f58f9`..`c49ea41` are on gitea only, by your kickoff choice.
   Public GitHub is behind by design.
2. **`sysctl -w vm.mmap_rnd_bits=28`** on the WSL2 host, if you want local ASan runs to stop
   failing ~25% of the time.
3. **`Recycle/2026-08-24-stray-dir-flight-semicolon-C`** — staged, not deleted. Yours to remove.
4. **G3.4a/G3.4d remain open**: `pfc_spectral.c:199` C3–C6, `pfc_image.c:309` C3/C4,
   `pfc_image.c:121` C4. The direct-call technique that worked for COLUMNAR applies.
5. **The three `TODO.md` items** are still blocked by D1; reopening them means revisiting it.
6. **`test_crosscheck.py:130`**'s CyCIF pool points at a dead session scratchpad, so that case
   silently never runs. Needs the Linux box to fix safely.
