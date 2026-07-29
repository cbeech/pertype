# Overnight report — flight-core toolchain-gated functionality

Started via /overnight. Branch: `flight-core`. Environment: Windows Git Bash at `C:\Dev\compression`
— NO native C toolchain available (no gcc/cc/make), Docker daemon not running. Per explicit user
caution ("computer crashed, do this later when it isn't so busy") and to avoid retriggering
instability, this run does NOT start Docker/WSL or attempt any native/heavy compilation. Scope is
limited to work verifiable by code review + config validation: a CI workflow that runs the
toolchain-gated gates (MISRA, libFuzzer, big-endian cross-compile+qemu) on a GitHub Actions runner,
which has root and the needed toolchains — none of that execution happens on this machine tonight.

**Never pushed. All commits stay local on `flight-core`.**

---
## Task 1: Review + integrate `flight/test/emit.c` — DONE

**Found a real bug via code review** (verified by C-language reasoning, not execution — no
toolchain available tonight): `emit.c` declared `static struct pfc_ctx g_work;`, but `pfc_ctx` is
forward-declared as an OPAQUE type in the public `pfc.h` (only `pfc_internal.h` has the full
struct). Instantiating an incomplete type at file scope is a hard C compile error — this file
would not have compiled as committed. `PFC_WORKMEM_BYTES` also expands to a function call
`(pfc_workmem_bytes())`, not a compile-time constant, so it can't size a static array either.

**Fix:** matched the established pattern already used correctly in `test_pfc.c`/`stress.c`/
`fuzz_pfc.c` — `static struct pfc_ctx *g_work;` + `g_work = malloc(pfc_workmem_bytes());` once at
startup (R2 governs the *library's* internal allocation, not a one-time caller-side setup).
Updated the misleading "static — no malloc" comment to explain this honestly, and to explain why
a literal static instantiation isn't possible against the public header.

Also verified by full read-through: `pfc_params` fields are fully set before each of the 3
`emit()` calls (image/seq/spectral), buffer sizes match `width*height*count*elemsize` exactly,
the LCG is deterministic (no time/rand), and the 4-byte length prefix is hand-packed
little-endian (not a host struct dump) — genuinely endian-portable on the writing side too.

Added a `build/emit` + `emit` Makefile target (mirrors the `build/test_pfc` pattern exactly;
verified real tabs via `cat -A`, no CRLF/space corruption). Documented in `README.md`.

**Verification status:** code-reviewed and reasoned correct with high confidence (opaque-type
and constant-expression rules are unambiguous C semantics); **NOT** locally compiled (no
toolchain in this shell tonight) — first real compile will happen in CI. Marked accordingly, not
claimed as tested.

Files: `flight/test/emit.c`, `flight/Makefile`, `flight/README.md`.

## Task 2+3: Author + validate `.github/workflows/flight-ci.yml` — DONE

New workflow, scoped to `flight/**` paths, triggers on push to `flight-core`/`master`/`main` + PRs
+ manual dispatch. Four independent jobs on `ubuntu-latest` (has root, apt):

- **native** — `make check` (strict -Werror, ASan/UBSan, R7 cross-check, decoder fuzz, 15k-case
  stress). Needs `build-essential` + `numpy` (verified by reading `test_crosscheck.py` directly:
  its only hard import beyond stdlib is `numpy`; the real-CyCIF/AVIRIS checks are wrapped in
  try/except or gated on `glob` finding nothing, so they skip cleanly on a CI runner with no
  local data — confirmed by reading the file, not assumed).
- **misra** — `apt-get install cppcheck` (prebuilt binary from the Ubuntu archive, NOT a from-
  source build — deliberately avoiding the failure mode that crashed a dev machine earlier
  tonight) then `make misra`, which already has the `--addon=misra --error-exitcode=2` logic.
- **libfuzzer** — `apt-get install clang`, builds `fuzz_pfc.c` with the EXACT flags documented in
  that file's own header comment, runs bounded to 120s wall-clock (`-max_total_time=120`) so the
  job terminates on its own; any crash/leak in that window fails it.
- **bigendian** — the real R4 proof. `apt-get install gcc-powerpc-linux-gnu qemu-user`, then:
  (1) build+run `emit.c` natively for an LE reference, `make clean`; (2) cross-compile BOTH
  `test_pfc` and `emit` with `CC=powerpc-linux-gnu-gcc CFLAGS='-O2 -static'` (matches the
  Makefile's own top-of-file documented invocation, extended to `build/emit`); (3) run the full
  139-check lossless test suite ON emulated big-endian PowerPC via `qemu-ppc` — proves no
  endian-dependent bug in the codec internals; (4) byte-compare the BE `emit` run against the LE
  reference via `cmp` — proves the *wire format itself* is canonical regardless of encoding-host
  endianness, the literal R4 claim.

  **Deliberate scope decision:** `crosscheck`/`fuzz_decode.py` and `stress.c` stay native-only
  (x86), NOT run under qemu. Reason: those Python scripts load `libpfc.so` via `ctypes`, and a
  same-arch Python interpreter cannot call into a cross-compiled foreign-architecture shared
  object — cross-compiling the `.so` for PowerPC would produce a library nothing on the runner
  could load. `stress.c`'s 150k-iteration fuzz loop would also just be slower under emulation for
  no proportional benefit once `test_pfc.c`'s 139 checks already validate BE correctness.

  **Also reasoned through (not just assumed) a real cross-arch data-model question:** the CI
  runner's LE reference build is 64-bit (`size_t`=64-bit); the BE cross-target is 32-bit PowerPC
  (`size_t`=32-bit). Verified by re-reading the codec internals: only fixed-width `uint32_t`/
  `uint16_t`/`uint8_t` values (packed via explicit byte-shifts, e.g. `pfc_put_u32`, and `emit.c`'s
  hand-packed length prefix) ever reach the wire format — `size_t` is used purely for in-memory
  indexing, never serialized. So a byte-identical result across the width difference is a
  meaningful, non-trivial proof, not a foregone conclusion — if a `size_t`-width-dependent bug
  existed, this comparison would catch it.

**Validation performed** (no CI available tonight, so validated everything I could without
execution): parsed the YAML with PyYAML — hit the well-known "`on:` parses as boolean `True`"
gotcha, and rather than assume it's harmless, cross-checked it against this repo's EXISTING,
already-working `ci.yml`, which shows the identical parsing shape — confirms it's a universal
PyYAML-vs-GitHub-Actions loader quirk, not a bug in the new file. Then did a full structured
walk of every job/step: confirmed every `run:` command references a Makefile target or file
invocation I'd independently verified exists (`make check`, `make misra`, `make build/emit`,
`make clean`, the exact `clang`/libFuzzer flags from `fuzz_pfc.c`'s own comment).

**Known uncertainty, flagged not hidden:** the apt package names (`gcc-powerpc-linux-gnu`,
`qemu-user`, `cppcheck`, `clang`) are standard Ubuntu/Debian conventions I'm confident in, but
unverified against the actual `ubuntu-latest` archive tonight (no apt-cache reachable from this
Windows shell). If a name is off, the affected job's `apt-get install` step fails cleanly and
visibly in CI logs — not a silent or destructive failure — and is a one-line fix.

**Verification status:** authored + reasoned correct with high confidence; **NOT run** — first
real execution happens when this branch is pushed. This workflow's existence is not evidence
the gates pass; a green run of it is.

File: `.github/workflows/flight-ci.yml` (new).

## Task 4: Update `requirements.md` + `mission-safety.md` — DONE

Updated both docs to honestly reflect the new CI workflow: every gate it covers (MISRA,
libFuzzer, big-endian) is now marked **"authored, not yet executed"** rather than left as a bare
"absent here" note or, worse, implied done. Added a new "CI: automating the toolchain-gated
gates" section to `requirements.md` explaining what each job does and why `crosscheck`/
`fuzz_decode.py`/`stress.c` deliberately stay native-only (documented reasoning, not just a
decision).

**Also found and fixed while reading closely:** `requirements.md` had a stray orphaned line
(`  feeding the existing arithmetic + mantissa coder).`) left over from an earlier edit, with no
matching opening text — dangling markdown fragment. Removed it.

**Also found `mission-safety.md` had drifted stale** relative to later work already reflected in
`requirements.md` — still said "4 codecs," "70 unit tests," "+1.5% vs CCSDS-121" (all superseded
by the spectral-codec work: 5 codecs, 139 unit tests, +2.8%). Freshened the evidence table and
bottom-line summary to match `requirements.md`'s current numbers, since I was already in the file
for the CI update and the drift was directly visible. Added an explicit honesty check I don't
think existed before: qemu-user emulation is real BE *execution* and a meaningfully stronger
check than static analysis, but it is **not** the actual RAD750/LEON/RISC-V target hardware or
RTOS — flagged that this remains a real gap even after the CI job goes green, rather than letting
"big-endian run: done" imply more than it will.

Renumbered "Prioritised next steps" to put "push flight-core and review the first CI run" at #1,
since that's now the literal unblock for several other items in the list.

**Verification status:** documentation-only changes, self-consistent by careful re-read (quoted
above). No execution involved or claimed.

Files: `flight/docs/requirements.md`, `flight/docs/mission-safety.md`.

## Task 2.5 (found mid-run, not in the original queue): add a Gitea Actions copy — DONE

**Real gap found by checking, not assuming:** `git branch -vv` confirms `flight-core` tracks
`origin/flight-core`, and `origin` is the self-hosted gitea (`ssh://git@10.0.0.246:222/...`) —
NOT github. GitHub only reads workflow files from `.github/workflows/`. Standing project policy
(from prior sessions) is that `flight-core` pushes to gitea only; GitHub stays untouched until
separately, explicitly asked. So as authored, `flight-ci.yml` would sit **completely dormant** on
this branch's actual home — a silent gap that would have made tonight's main deliverable
functionally inert until some unrelated future decision to push to GitHub.

Added `.gitea/workflows/flight-ci.yml` — verified byte-identical job bodies to the GitHub copy
(diffed the two files; only the `paths:` filter differs, pointing each at its own file).
Validated with the same PyYAML structural check as the original (4 jobs parse correctly).

**This copy carries real, flagged uncertainty the GitHub one doesn't**, documented in its own
header rather than glossed over: whether Gitea Actions is enabled on this instance at all, whether
a registered runner advertises the `ubuntu-latest` label this file assumes, and whether the
runner has outbound internet access for `apt-get`/`actions/checkout`. None of this was verifiable
tonight (would require probing the gitea server, which I did not do, or pushing, which is
prohibited). If any assumption is wrong, every job fails visibly and immediately at its first
network step — not a silent or destructive failure, and a one-line fix once the actual runner
config is known.

I judged this in-scope rather than invented scope: it's the same original deliverable
("implement toolchain-gated functionality") correctly targeting the environment this branch
actually pushes to, discovered through legitimate review of the repo's own remote configuration —
not a new task.

Files: `.gitea/workflows/flight-ci.yml` (new), `flight/docs/requirements.md` (noted the two-copy
setup and the extra uncertainty).

---

## Summary (morning wrap-up)

**Goal:** implement toolchain-gated functionality for flight-core (MISRA, libFuzzer, big-endian
cross-compile+qemu) — continuing from a prior session that ended in a machine crash mid-way
through a from-source cppcheck build.

**Environment constraint that shaped everything:** this run executed in a Windows Git Bash shell
with **no C toolchain at all** (no gcc/cc/make) and Docker's daemon not running. Combined with
your explicit caution twice ("computer crashed, do this later when it isn't so busy"), I ruled out
starting Docker/WSL or any native compilation tonight. Every deliverable below is therefore
**authored and reasoned correct by careful code review — not executed, not proven by a test run.**
That distinction is stated explicitly in every file touched, not left implicit.

### Done (all committed locally, all local-only — nothing pushed)

| Commit | What |
|---|---|
| `18a4fb5` | Found + fixed a real compile bug in uncommitted `flight/test/emit.c` (incomplete-type instantiation); wired into the Makefile |
| `74762d3` | Authored `.github/workflows/flight-ci.yml` — 4 jobs: native (`make check`), misra (apt cppcheck), libfuzzer (clang, bounded), bigendian (qemu-user BE execution + R4 wire-format proof) |
| `78d634b` | Updated `requirements.md`/`mission-safety.md` to honestly reflect the new CI workflow; fixed a stray orphaned doc line; freshened stale numbers found along the way |
| `49631f4` | Found `flight-core` actually pushes to gitea, not GitHub — added an equivalent `.gitea/workflows/flight-ci.yml` with its own flagged uncertainty |

Plus: memory note (`flight-core-libpfc.md`) and `HANDOFF.md` updated outside git to point at
tonight's state for the next session.

### Verification evidence for each "done" item
- `emit.c` fix: verified by C-language reasoning (opaque-type/constant-expression rules are
  unambiguous), cross-checked against 3 other files in this repo that already use the correct
  pattern successfully. NOT compiled.
- CI workflows: verified by PyYAML structural parsing (both files), a full manual trace of every
  `run:` command against Makefile targets/invocations I independently confirmed exist by reading
  source, and a diff proving the two workflow copies' job bodies are identical. NOT executed.
- Docs: verified by careful re-read for internal consistency after editing. Not applicable to
  "execution" — these are honest-status writeups, and I checked they don't overclaim.

### Parked / not attempted
Nothing was started and abandoned tonight — the environment constraint (no toolchain, Docker
down) was discovered during Phase 1 kickoff, before committing to any task that would have needed
it, so the queue itself was scoped to avoid a parked item. Nothing to report here.

### Decisions waiting on you
1. **Review the 4 commits above**, especially the `emit.c` bug fix and the two CI workflow files.
2. **Push `flight-core` to gitea (`origin`)** when ready — this is the actual unblock: it's the
   only way any of tonight's CI-related work gets its first real execution and stops being "authored,
   not yet executed."
3. **After the first CI run**, come back and update `requirements.md`/`mission-safety.md` with
   real results (pass/fail per job) — I've deliberately left every relevant claim phrased so this
   update is a clean find-and-replace of "authored, not yet executed" with the actual outcome.
4. **Gitea Actions may not even be enabled** on the self-hosted instance, or the runner's label/
   network access may not match what `.gitea/workflows/flight-ci.yml` assumes — this can only be
   resolved by trying it (or checking the gitea admin panel), not from this environment tonight.
5. Separately, when convenient: a from-source cppcheck build was abandoned on a prior crashed
   session in `/tmp` on the Linux/WSL side of this project — worth confirming nothing was left in
   a bad state there, unrelated to tonight's Windows-side work.

Nothing was started tonight that needs stopping (no servers, no background processes).
