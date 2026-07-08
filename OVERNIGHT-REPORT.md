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

