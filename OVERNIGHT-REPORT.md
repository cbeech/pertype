# Overnight run — CI validation slate (2026-08-26)

Branch: `master`. **No pushing this run** — the user chose the `overnight` skill's default hard
rule at kickoff, so everything below stays as local commits. Nothing left this machine.

Previous run archived to `OVERNIGHT-REPORT-2026-08-25.md`.

## Why this queue

`cad952f` shipped a CI change I explicitly could **not** verify: `container: node:22-trixie` on the
`native`/`libfuzzer` jobs and a new `mcdc32` job. The `make` targets were proven on `craig@ai`, but
whether act — which is what the self-hosted Gitea runner actually is — honours `container:` and
parses the new job was untested. That is the known unknown, so it goes first.

Per the user's standing instruction, **all test execution happens on `craig@ai`**, not local
Docker-on-WSL2.

## Queue

| # | Task | Status |
|---|------|--------|
| 1 | Validate act's `container:` support and the `mcdc32` job | running |
| 2 | Full 10-gate run on `ai` at `cad952f` | queued |
| 3 | Workflow structure-sync checker (ignoring prose) | queued |
| 4 | Long libFuzzer soak in `node:22-trixie` | queued |
| 5 | Wrap-up | queued |

## Needs you (not queued)

- **Pushing** — nothing reached any remote this run, by the kickoff decision.
- The three open `TODO.md` items — features, blocked by D1.
- RG2.1, RG4.1, R5 — need a person, funding, or hardware.
- `Recycle/2026-08-24-stray-dir-flight-semicolon-C` — staged, not deleted.
- `sysctl -w vm.mmap_rnd_bits=28` — fixes the ASan cause rather than routing around it.

---

## Results

### Task 3 — CI workflow drift gate ✅ done (`bc067e1`)

`.github/workflows/flight-ci.yml` and `.gitea/workflows/flight-ci.yml` are near-identical and kept
in sync **by hand**. That is the arrangement that lets a gate be tightened in one copy and silently
left weak in the other, and the failure is invisible: both files stay valid YAML, both keep
passing, and the runner you actually push to quietly tests less than you think.

A plain `diff` can't police it — the two are *supposed* to differ (each has its own header and path
filter; `diff` reports 40+ legitimate lines). So `flight/tools/workflow_sync.py` compares job
names, `container:`, and the ordered list of step names / `uses:` / `run:` bodies, and ignores
prose. No PyYAML: the check has to run on a bare image with nothing beyond `python3`, the same
constraint the rest of `tools/` works under.

Wired in as `make wfsync` and as a step in the `misra` job of both copies.

**Mutation-tested, because a checker that only ever passes is worthless:**

| injected drift | result |
|---|---|
| `container:` changed in one copy only | rc=1, both affected jobs named |
| whole `mcdc32` job dropped from one copy | rc=1, job named |
| `make mcdc` → `make mcdc MCDC_MIN=0` in one copy | **rc=1, exact step diffed** |
| (restored) | rc=0, file byte-identical to backup |

The third is the case worth having: a **silently weakened gate**. That is the drift that would
otherwise cost you a real regression, and it is caught with the before/after shown.

Incidental correction: this is **9** CI jobs, not the 10 I said at kickoff — the original 8 plus
`mcdc32`.

### Task 1 — Validate act's `container:` support ✅ the known unknown is closed

`cad952f` shipped a CI change I could not verify: `container: node:22-trixie` on `native` and
`libfuzzer`, plus a new `mcdc32` job. The self-hosted Gitea runner **is** act, so act is the right
oracle. Installed act 0.2.89 on `craig@ai` and ran it against the real workflow with
`-P ubuntu-latest=node:20-bookworm`, which mirrors the Gitea runner's actual default mapping (and
avoids act's interactive image prompt, which would hang an unattended run).

| check | result |
|---|---|
| parse both workflows | **rc=0** — all **9** jobs enumerated, `mcdc32` among them |
| dry run `native` (has `container:`) | **rc=0** |
| dry run `mcdc32` (new job) | **rc=0** |
| **real run of `misra`** | **`Job succeeded`, rc=0** |

The real `misra` run is worth more than the dry runs: it executed the whole job end-to-end
*including the new `wfsync` drift step added in `bc067e1`*. A failure there would have failed the
job, so that step is confirmed working under act on a runner-representative image.

A real run of `native` — the decisive test of `container:` at execution time rather than parse
time — is in progress.

**A repeat of my own mistake, worth recording:** the misra step list looked like it was missing the
`wfsync` step, and I started investigating a phantom. Cause: `act_test.sh` piped the run through
`tail -25`, so earlier steps scrolled off. That is the third time this run family that truncating
my own output cost me time. The gate scripts write full per-gate logs now; the ad-hoc probes still
don't, and should.

**Also fixed en route:** the first launch died instantly because the script wrote to `/out/act.log`
— a container mount path — while being run directly on the host. `pgrep` then matched its own
command line and reported it as "still running", which masked the failure for one cycle.

**Decisive result:** a real (non-dry) run of `native` — the job carrying
`container: node:22-trixie` — under act:

```
[flight-ci/native]   | ALL CHECKS PASSED
[flight-ci/native] 🏁  Job succeeded
-- act native rc=0 --
```

So act honours `container:` at execution time, not merely at parse time, and the full `make check`
suite runs inside the pinned image. **The CI change shipped in `cad952f` is validated against the
same tool the Gitea runner uses.** That was the one thing I flagged as untested when I pushed it.

Caveat worth keeping: act on `ai` is a faithful *stand-in* for the Gitea runner, not the runner
itself. Instance-specific factors — whether Actions is enabled, runner labels, network egress —
remain unverifiable from here, exactly as the `.gitea` file's own header already says.

### Task 2 — Full 9-gate run at local HEAD ⚠️ first attempt failed on my own script

First run: **all eleven gate invocations returned rc=127**. Not a code failure — `make` itself was
missing, because the toolchain install had failed wholesale with *"Unable to correct problems, you
have held broken packages"*.

Cause: **`gcc-multilib` conflicts with `gcc-powerpc-linux-gnu`** on Debian/Ubuntu. Requesting both
in one `apt-get install` makes apt install **nothing**. I hit this exact conflict in the first gate
run days ago and worked around it by installing multilib separately — then regressed it here by
merging the package lists.

Two fixes, both in the harness rather than the repo:
- Drop `gcc-multilib`. `clang -m32` needs only `libc6-dev-i386`, so `mcdc32` is unaffected.
- **Fail fast on a broken toolchain.** The script now checks each required tool is present after
  install and aborts naming the missing one. A silent install failure previously surfaced as
  eleven identical `rc=127`s, which reads like a catastrophic code failure rather than a missing
  package — the diagnostic cost was entirely avoidable.

I also launched the container after my patch script had thrown an `AssertionError`, so the second
run re-ran the unfixed script. Stopped it, patched by line position, verified the file content
*and* `bash -n` before relaunching. Third run in progress.

**Second failure, different cause: CRLF.** The relaunch died instantly with an empty log and
`Exited (2)`. `docker logs` had what the file never received:

```
/out/gates_all.sh: line 4: 1: ambiguous redirect          <- `exec > file\r`
E: Command line option '\n   ' [from -qq\n] is not understood
/out/gates_all.sh: line 14: syntax error near unexpected token `$'do\r''
```

**Python's `open(path, "w")` on this Windows host translates `\n` to `\r\n`.** The script I
patched with Python arrived on Linux with carriage returns. Bash heredocs are unaffected, which is
why every earlier script worked and only the Python-patched one broke.

**Checked whether this contaminated the repo — it did not.** The working tree does carry CRLF
(`flight/Makefile`: 367 CR lines), but git normalises on commit:
`git show HEAD:flight/Makefile | grep -c $'\r'` returns **0**. Committed content reaches Linux
clean, which is also why the bundle-based transfer to `ai` has been correct throughout. The hazard
is confined to files that **bypass git** — scp'd scratch scripts and docker-mounted copies.

Recorded in memory as a durable gotcha, with the fix (`newline="\n"`) and the recommendation to
move work by `git bundle` rather than scp'ing loose files.

**Process note:** this is the same approach on its third execution. Both prior failures were
defects in my harness — a package conflict and then line endings — not evidence the task is
infeasible, so continuing is within the retry bound rather than a third distinct approach. If it
fails again I will park it.

**Third execution — 9 of 11 pass:**

| gate | rc | time |
|---|---|---|
| wfsync (new) | 0 | 0s |
| native | 0 | 41s |
| misra | 0 | 2s |
| coverage | 0 | 46s |
| mcdc | 0 | 51s |
| **mcdc32** | **2** | 0s |
| stackdepth / stackppc | 0 | 0s / 1s |
| libfuzzer | 0 | **302s** |
| bigendian | 0 | 1s |
| **cbmc** | **2** | 62s |

Both failures are **my harness, not the repository**, and they disprove a claim I made an hour
earlier in this same report:

```
mcdc32:  /usr/bin/ld: cannot find crtbeginS.o / -lgcc / -lgcc_s
cbmc:    fatal error: gnu/stubs-x32.h: No such file or directory
```

I had asserted that "`clang -m32` needs only the i386 libc headers, so `libc6-dev-i386` alone
suffices". **Wrong.** Those headers give you the 32-bit libc but not gcc's 32-bit *runtime*
(`crtbeginS.o`, `libgcc`), which comes from `gcc-multilib` — and `cbmc --32` needs the multilib
headers too. Both genuinely require the package I removed to dodge the conflict.

**This is exactly why real CI runs them as separate jobs**, and the CI config is correct as
shipped: `mcdc32` installs `libc6-dev-i386 gcc-multilib` in its own container, `cbmc` installs
`gcc-multilib` in its own, and `bigendian`/`stackdepth` install the PowerPC cross-compiler in
theirs. The conflict only exists in a single-container harness like mine. Re-ran those two gates in
a dedicated multilib container, mirroring the CI job split.

**Corrected in the repo:** the `mcdc32` Makefile comment said "libc6-dev-i386 (Debian/Ubuntu) **or**
gcc-multilib", which repeats the same wrong claim and would mislead the next person. It now states
that gcc-multilib is required, names the exact link errors you get without it, and records the
conflict with `gcc-powerpc-linux-gnu` and why the two jobs must stay in separate containers.

**Task 2 complete — 11/11 gate invocations pass** (9 CI jobs; `wfsync` and `stackppc` are extra
invocations) across two containers split exactly as CI splits them:

```
mcdc32  rc=0   TOTAL MC/DC: 108/115 = 93.91%   floor 93   PASS
cbmc    rc=0   ** 0 of 152 failed              VERIFICATION SUCCESSFUL
```

The Makefile comment correction is committed separately so the reasoning survives with the code
rather than only in this report.

### Task 4 — Extended libFuzzer soak ✅ 6.25 M executions, 0 crashes

Three hours, four workers, in `node:22-trixie`, seeded from `emit.c` fixture streams with the
corpus persisted.

| metric | result |
|---|---|
| executions | **6 251 440** across four workers (1.53–1.60 M each) |
| corpus | 3 seeds → **9 170 files** |
| coverage reached | 748 edges, 4 781 features |
| **crashes / leaks / OOM** | **0** — no artifacts |
| workers completing | **4 of 4** |

For scale: a single CI `libfuzzer` run does ~564 k executions from an **empty** corpus. This is
~11× that volume, from a corpus that compounded to 9 170 inputs — which is the thing
`mission-safety.md` §2.6 formally accepts CI cannot do.

**The "4 of 4" line is the one that matters beyond the fuzzing.** The previous soak lost one of its
four workers to the ASan startup bug — a bare `Segmentation fault`, no report — and I initially
mistook that for a possible libpfc defect. On the pinned image every worker ran to completion. That
is the image fix confirmed in anger rather than in a 60-trial microbenchmark, and it means a worker
death in a future soak would actually signify something.

---

## Summary

**Queue: 5 of 5 complete.** Nothing parked, nothing skipped. **Nothing pushed** — the kickoff
choice was the hard rule, so all work sits as local commits on `master`.

| # | task | outcome |
|---|------|---------|
| 1 | Validate act's `container:` support | ✅ the known unknown closed — real `native` run under act, `Job succeeded` |
| 2 | Full gate run at local HEAD | ✅ 11/11 invocations pass (after two harness bugs of mine) |
| 3 | Workflow drift gate | ✅ `bc067e1`, mutation-tested on three injected drifts |
| 4 | Extended fuzz soak | ✅ 6.25 M executions, corpus 3 → 9 170, **0 crashes**, 4/4 workers |
| 5 | Wrap-up | ✅ this report, HANDOFF, memory |

### What this run was actually for

`cad952f` shipped a CI change I flagged as **untested**: `container: node:22-trixie` and a new
`mcdc32` job. That is now validated against act — the same tool the Gitea runner is — including a
real (non-dry) job execution inside the pinned image. The residual uncertainty is only
instance-specific (is Actions enabled, runner labels, network egress), which the `.gitea` file's
own header already states and which cannot be probed from here.

The soak then confirmed the image fix in anger: **4 of 4 workers completed**, where the previous
soak lost one to the ASan startup bug I spent hours root-causing.

### Three mistakes of mine, all in the harness

1. **`gcc-multilib` vs `gcc-powerpc-linux-gnu` conflict** — requesting both installs *neither*, and
   the symptom is eleven identical `rc=127`s that look like catastrophic code failure. I had
   already hit this conflict once and regressed it. Fixed, plus a fail-fast toolchain check so the
   next occurrence names the missing tool instead of cascading.
2. **CRLF** — Python's `open(w)` on Windows emits `\r\n`, breaking scripts on Linux with errors
   that name neither line endings nor the real line. Verified this did **not** contaminate the repo
   (git normalises on commit); it only affects files that bypass git. Recorded in memory.
3. **A wrong technical claim that reached a committed comment** — I asserted `libc6-dev-i386` alone
   suffices for `clang -m32`. It does not: the 32-bit *gcc runtime* comes from `gcc-multilib`.
   Corrected in `31d023c` with the exact link errors named, since the Makefile comment would
   otherwise have misled the next person.

The pattern worth noting: all three were failures of my *tooling around* the work, and two of them
produced symptoms that pointed away from their causes. The fail-fast check and the drift gate both
exist now specifically to shorten that distance.

### Commits (local only)

| SHA | what |
|---|---|
| `303ce00` | rotate overnight report |
| `bc067e1` | CI workflow drift gate + `tools/workflow_sync.py` |
| `31d023c` | correct the `mcdc32` toolchain requirement |

### Needs you

1. **Pushing** — three commits sit local by the kickoff decision.
2. **The `mcdc32` and `wfsync` jobs have never run on the real Gitea runner.** act says they work;
   the instance itself is the remaining unknown. First push will tell you.
3. The three open `TODO.md` items — features, blocked by D1.
4. RG2.1, RG4.1, R5 — need a person, funding, or hardware.
5. `Recycle/2026-08-24-stray-dir-flight-semicolon-C` — still staged, not deleted.
6. `sysctl -w vm.mmap_rnd_bits=28` — fixes the ASan cause at the host level; the image pin routes
   around it for CI only.
