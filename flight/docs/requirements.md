# libpfc — requirements & traceability

Flight-software requirements for the pertype-flight lossless core, each with a verification method
and the test(s) that exercise it. IDs are stable; tests live in `flight/test/` and `flight/ground/`.

## Requirements

| ID | Requirement | Verification |
|----|-------------|--------------|
| **R1** | **Lossless.** For every valid input, `decode(encode(x)) == x` byte-for-byte. | Test (round-trip, all 4 codecs, synthetic + real) |
| **R2** | **No dynamic allocation.** No `malloc`/`free`/recursion. Working memory is a caller-supplied `pfc_ctx`. | Inspection (no alloc symbols in the `.so`) + test |
| **R3** | **Bounded memory.** Working set ≤ compile-time maxima; footprint = `pfc_workmem_bytes()`. | Inspection + test |
| **R4** | **Deterministic & portable.** Integer-only; canonical little-endian wire format (big-endian encoder ⇄ little-endian decoder). | Inspection (no FP in `src/`) + independent LE decoder |
| **R5** | **No expansion.** Output never exceeds `pfc_bound()`; incompressible blocks store raw. | Test (random data within bound, all codecs) |
| **R6** | **Error containment.** Each block independently CRC-protected; a corrupt/truncated frame loses one block, is reported, never reads OOB or crashes. | Test (bit-flip + truncation) + ASan/UBSan + fuzz |
| **R7** | **Independent reference.** An independent implementation decodes the C encoder's output byte-for-byte. | Cross-check (pure-Python ground decoder vs C encoder) |

## Codec coverage (broad scope)

| Codec | Front-end | Status |
|-------|-----------|--------|
| `PFC_CODEC_IMAGE` | 2-D MED/LOCO-I predictor + bias correction + gradient context + run mode | ✅ |
| `PFC_CODEC_SEQ` | 1-D order-1 delta (int8/16/32, signed/unsigned) | ✅ |
| `PFC_CODEC_FLOAT` | float32/64 byte-plane split | ✅ |
| `PFC_CODEC_COLUMNAR` | record de-interleave + per-plane delta | ✅ |
| `PFC_CODEC_SPECTRAL` | multi/hyperspectral cube: inter-band MED-of-difference prediction | ✅ |

All five share one integer range coder, one adaptive category model, and one block-framing layer.

## Traceability matrix

| Requirement | Test / evidence | Status |
|-------------|-----------------|--------|
| R1 Lossless | `test_pfc.c` (image8/16, seq, float, columnar, odd sizes); `bench_real.py` (real CyCIF 4/4 byte-exact) | ✅ verified |
| R2 No malloc | `src/` uses only `stddef/stdint`; `nm -uD build/libpfc.so` shows **no alloc imports** | ✅ verified |
| R3 Bounded memory | `pfc_workmem_bytes()` = 329 096 B at defaults; tune via `-DPFC_MAX_COLS`/`-DPFC_BAND_ROWS` | ✅ verified |
| R4 Deterministic/portable | no `float`/`double` types in `src/`; the **explicit-LE pure-Python decoder** decodes the C output (`test_crosscheck.py`), and store-raw serialises LE → stream is canonical | ✅ stream verified; **BE-hardware run: CI workflow authored (`.github/workflows/flight-ci.yml`, `bigendian` job), not yet executed** |
| R5 No expansion | `test_pfc.c` random inputs (all codecs) stay ≤ `pfc_bound`; store-raw path exercised | ✅ verified |
| R6 Error containment | `test_pfc.c::test_fault_injection`/`::test_truncation`; `make asan` clean; **`fuzz_decode.py` 20 000 random/mutated inputs, no crash/OOB** | ✅ verified |
| R7 Independent reference | `test_crosscheck.py`: C-encode → Python-decode == original, **10/10** incl. real CyCIF, AVIRIS spectral, flat run-mode, all 4 codecs | ✅ verified |

## Verification environment (this build)

- `make strict` — `-std=c99 -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wstrict-prototypes -Werror`: **139/139, zero warnings**.
- `make asan` — ASan + UBSan: **139/139, no diagnostics**.
- `test_crosscheck.py` — C encoder vs independent Python decoder: **10/10 byte-exact** (incl. real CyCIF).
- `fuzz_decode.py` — **20 000 iterations**, decoder survived all random/mutated input.
- `ccsds_compare.py` — real CyCIF 16-bit: **pfc 1.76× beats CCSDS-121-class Rice (1.71×) by +2.8%** on the same MED predictor; within **−1.3%** of JPEG-LS at the default band (−0.5% at band=64).

### Context-adaptation study (closing the JPEG-LS gap)

Sweeping band size (the `-DPFC_BAND_ROWS` knob) on real CyCIF separated two effects:

| band | workmem (MAX_COLS=8192) | vs JPEG-LS |
|------|-------------------------|------------|
| 16 (default) | 322 KB | −1.3% |
| 32 | 578 KB | −0.9% |
| 64 | 1.0 MB | −0.5% |
| whole-image | 17 MB | ≈ −0.3% |

Findings: (1) **per-band model resets** (needed for error-containment) cost ~1% via adaptation
dilution — recoverable by larger bands, a memory/containment tradeoff (with instrument-matched
`MAX_COLS` the cost is far lower, e.g. ~160 KB for a 1280-wide sensor at band 64). (2) **Modelling
the top mantissa bit** adaptively per category is a general win (−0.3 to −0.4% here, and large on
prediction-residual codecs: seq-ramp 15×→261×, columnar 11×→14×). (3) A directional *entropy*
context **regressed** on photon-noisy data (dilution, no directional payoff) and was dropped — the
residual sub-percent floor is finer context modelling that only pays off with bigger bands.
- `make stress` — randomised property + edge + negative + fuzz under ASan/UBSan: **15 063 cases
  (5000 random round-trips across all codecs + adversarial edges + 150 000 decode-fuzz), 0 failures**.
  *This run found and fixed a real defect:* `pfc_bound()` under-estimated the per-block framing
  overhead for skinny incompressible images (e.g. 1×30000 8-bit), so a caller sizing to `pfc_bound`
  could get a spurious `PFC_E_BOUND`. Fixed to account for the worst-case block count; re-verified.
- `make misra` — `cppcheck --addon=misra` gate. Not run locally (cppcheck absent, and a from-source
  build was deliberately abandoned mid-run after it contributed to a machine crash — see the
  `misra` job in `flight-ci.yml`, which installs cppcheck from the Ubuntu archive instead of
  building it). **Authored, not yet executed.**
- `fuzz_pfc.c` — libFuzzer harness. Not run locally (no clang in this environment). Wired into the
  `libfuzzer` job in `flight-ci.yml` (bounded to 120s wall-clock). **Authored, not yet executed.**

### Structural coverage (gcov, full corpus = test_pfc + stress)

| File | Lines | Branches taken |
|------|-------|----------------|
| pfc_arith.c (range coder) | 100% | 95% |
| pfc_model.c | 100% | 100% |
| pfc_crc.c / pfc_frame.c | 100% | 100% |
| pfc.c (dispatch) | 98.6% | 84% |
| pfc_image.c (incl. bias + run mode) | 99.2% | 93% |
| pfc_seq.c | 97.2% | 86% |
| pfc_columnar.c | 96.8% | 88% |
| pfc_spectral.c | 96.1% | — |

Driving coverage exposed that the `seq`/`float`/`columnar` **error-containment paths had no direct
tests** (only `image` did); targeted bit-flip + truncation + malformed-header + invalid-param tests
were added for every codec (`test_validation`, `test_corruption_all`, `test_more_guards`). The
residual ~9 uncovered lines are **defensive guards verified by inspection**: (a) internal
re-validation the dispatcher already enforces; (b) the raw-block plen-mismatch repair, reachable
only with a CRC-valid-but-length-inconsistent block (negligible from real corruption) and
structurally identical to the tested CRC-mismatch repair. MC/DC measurement is a future step.

See `docs/mission-safety.md` for the gap between this evidence and formal flight qualification.

## CCSDS-123 comparison (hyperspectral) — `ccsds123_compare.py`

On real AVIRIS Indian Pines (200 contiguous bands, 145×145, 16-bit), with the entropy coder held
constant (block-adaptive Rice):

| predictor | ratio |
|-----------|-------|
| per-band MED (spatial only) | 2.03× |
| inter-band delta (naive spectral) | 1.88× — *worse* |
| CCSDS-123-class (spectral+spatial least-squares) | **2.37×** |

Real codecs: pfc per-band 2.03×, JPEG-LS 2.02×, **pfc SPECTRAL 2.35× (−1.2% vs CCSDS-123-class)**.

The `PFC_CODEC_SPECTRAL` codec **closed the gap from −16.9% to −1.2%** — near parity with the
CCSDS-123-class predictor — using a simple integer MED-of-difference predictor (predict the
inter-band difference image spatially) feeding the existing arithmetic + mantissa coder. Findings:
(1) on hyperspectral, exploiting inter-band correlation is the whole game; (2) **naive inter-band
delta hurts** (bands have a scale/offset) — but MED on the *difference image* captures the
gain-induced spatial structure that naive delta misses; (3) pfc's arithmetic coder ≈ Rice on this
data, so the predictor is everything. (The CCSDS-123-class bar is a per-band least-squares rendition
of the standard's adaptive spectral+spatial predictor — class-faithful, not bit-exact. Lossless
round-trip and C↔Python cross-check verified on real AVIRIS bands.)

## CI: automating the toolchain-gated gates — `flight-ci.yml`

The three items below all needed a toolchain (cppcheck, clang, a PowerPC cross-compiler + qemu)
absent from every environment this project has been developed in so far. Rather than install them
on a dev machine (a from-source cppcheck build was abandoned mid-build after contributing to a
machine crash), a CI workflow now runs all three on a fresh `ubuntu-latest` runner (has root +
apt, so prebuilt packages install cleanly — no from-source builds needed).

**Two copies, same content:** `.github/workflows/flight-ci.yml` (GitHub Actions) and
`.gitea/workflows/flight-ci.yml` (Gitea Actions). `flight-core`'s actual push target is `origin`
(the self-hosted gitea), not GitHub — GitHub only reads `.github/workflows/`, so without the
gitea copy the GitHub version would sit dormant on this branch. The gitea copy carries
**materially higher uncertainty**, flagged in its own header: whether Gitea Actions is even
enabled on this instance, whether a runner advertises the `ubuntu-latest` label used here, and
whether the runner has outbound internet access at all (needed for `apt-get` and to fetch
`actions/checkout`). If any of that is wrong, every job fails visibly at its first network step —
not silently. Keep the two files in sync by hand; job bodies are otherwise identical.

- **`native` job** — `make check` (the full local gate: strict, ASan/UBSan, R7 cross-check,
  decoder fuzz, 15k-case stress).
- **`misra` job** — `apt-get install cppcheck` (prebuilt binary, not a source build) → `make misra`.
- **`cbmc` job** — `apt-get install cbmc` → `make cbmc`: a bounded model-checking proof (not a
  test) that `pfc_block_read` — the shared block-framing primitive every codec's decoder calls to
  parse untrusted downlink bytes — never reads out of bounds. Run with `--32` so CBMC models a
  32-bit `size_t`, matching the real flight targets (RAD750/LEON/RISC-V-32) rather than the
  64-bit host every other job here runs under; see `proofs/cbmc/harness_block_read.c` and the
  "CBMC proof" section below for why that distinction is the entire point of this gate.
- **`libfuzzer` job** — `apt-get install clang`, builds `fuzz_pfc.c` per its own documented
  invocation, runs bounded to 300s wall-clock (raised from an initial 120s once the gate proved
  its value — see below). The corpus persists across CI runs via `actions/cache` (a `libfuzzer-
  corpus-<run_id>` key with `restore-keys` grabbing the latest prior one — the standard "ratchet"
  cache pattern), so fuzzing coverage compounds run over run instead of restarting from nothing
  each time. A crash artifact uploads automatically on failure for easy local reproduction.
- **`bigendian` job** — installs `gcc-powerpc-linux-gnu` + `qemu-user`, then (1) runs the full
  139-check `test_pfc` suite ON emulated big-endian PowerPC hardware (proves no endian-dependent
  bug in the codec internals — genuinely the "big-endian hardware run" this section used to list
  as an open item), and (2) byte-compares `emit.c`'s output built once natively (LE) and once
  cross-compiled (BE) via `cmp` (the literal R4 wire-format proof). `crosscheck`/`fuzz_decode.py`
  and `stress.c` deliberately stay native-only: ctypes can't call into a cross-compiled foreign-
  architecture `.so`, and `stress.c`'s 150k-iteration loop gains nothing from emulation once
  `test_pfc` already covers BE correctness.

**Status: pushed and executed for real (first run: #15/#16 on the `ugreen-nas` Gitea Actions
runner).** The overnight-authored workflow was reasoned-correct but unexecuted; the first actual
run surfaced real, useful results — both environment bugs in the workflow itself and a genuine
memory-safety bug in the code:

- **`bigendian` — ✅ PASSED for real.** The full 139-check `test_pfc` suite ran on emulated
  big-endian PowerPC via `qemu-ppc` and the `emit.c` LE-vs-BE byte-comparison matched. This is the
  actual R4 execution proof, not just the wire-format reasoning — genuinely closes that open item.
  Also confirms the runner has real outbound internet access (apt archive + GitHub for actions),
  resolving the biggest flagged unknown from the overnight authoring.
- **Two environment bugs found + fixed:** (1) the runner's `ubuntu-latest` label maps to
  `node:20-bookworm` (act's own default for an unconfigured self-hosted runner, not a real Ubuntu
  image) — it runs as root already and has no `sudo` binary, so every job died in ~10s on the
  first `sudo apt-get` line; fixed by dropping `sudo` (jobs run as root in this container). (2)
  `actions/setup-python@v5` can't find a matching Python 3.12 build for this runner's self-
  reported platform (a known friction point for `act`-based self-hosted runners against
  GitHub's hosted-runner-oriented version manifest); fixed by installing `python3`/`python3-numpy`
  directly via `apt-get` instead (Debian-based image, no PyPI/manifest lookup needed).
- **`misra` — ran successfully end-to-end** (`apt-get install cppcheck` → `make misra`) and
  correctly **failed the build on 179 real MISRA-C:2012 findings across 17 rules** — the gate
  itself works correctly. **Triage now done — see `flight/docs/misra-deviations.md` for the full
  rule-by-rule record.** Summary: 4 findings were genuine and trivially fixed (two missing `else`
  branches, one loop-variable reuse); 27 were confirmed cppcheck-MISRA-addon false positives
  (comma-separated declarator lists misidentified as the comma operator, and cross-file function
  usage the single-file scan can't see) — suppressed with a documented reason; the remaining 148
  are deliberate, verified-safe patterns that conflict with an Advisory rule (early-return
  defensive style, explicit widening casts, void* buffer handling, etc.) — each spot-checked
  against real source, not assumed, and suppressed via `flight/.cppcheck-suppressions` with a
  written justification per rule. `make misra` now applies that suppression list, so the gate
  stays meaningful (anything not on the reviewed list still fails the build) rather than silenced.
- **`libfuzzer` — found a real heap-buffer-overflow within ~2 minutes of fuzzing.** ASan:
  `SUMMARY: AddressSanitizer: heap-buffer-overflow flight/src/pfc_internal.h:134:22 in
  pfc_get_u32`, called from `pfc_spectral_decode` (`pfc_spectral.c:219`). Root cause: SPECTRAL's
  header is `PFC_SPEC_HDR` (24 bytes) but the top-level `pfc_decode()` dispatcher only validates
  `src_len >= PFC_HDR` (20 bytes) before routing to a codec — correct for the other four codecs,
  whose header *is* exactly `PFC_HDR`, but insufficient for SPECTRAL. A 20–23-byte crafted input
  (`PFC1`+ver+codec=5+garbage) passed that check and `pfc_spectral_decode` read `s[20..23]`
  unconditionally, one byte past the buffer. **Fixed:** added an explicit
  `if (len < PFC_SPEC_HDR) return PFC_E_CORRUPT;` at the top of `pfc_spectral_decode`, before any
  header byte is touched — matches how the other codecs rely on (and satisfy) the dispatcher's
  generic check, since only SPECTRAL's header size differs. Added a regression test in
  `test_pfc.c` (`test_spectral_corruption`) using the exact crashing input. **Notable:** the
  Python-based `fuzz_decode.py` ran 20,000 iterations in the earlier overnight session and found
  nothing — libFuzzer's coverage-guided mutation found this in under 2 minutes, a concrete
  demonstration of why the C/ASan/libFuzzer gate (not just the Python harness) is worth having.
- **`native` — ✅ FIXED and confirmed on the next run.** The `setup-python` fix (installing
  `python3`/`python3-numpy` via `apt-get` instead) worked: `make check` — strict `-Werror` build,
  ASan/UBSan, the R7 independent-decoder cross-check, decoder fuzz, and 15k-case stress — all
  passed in ~73s on the second run.
- **`libfuzzer` — found a SECOND real bug immediately after the SPECTRAL fix, in the very same
  120s run.** With the SPECTRAL crash gone, fuzzing continued (visible in the log: coverage climbed
  past 1,088,000 executions) and found a heap-buffer-overflow in `pfc_columnar_decode`
  (`pfc_columnar.c:150`, both ASan and a UBSan `index 65536 out of bounds for
  uint8_t[65536]` on the same line). Root cause: `pfc_columnar_encode` always derives
  `block_recs = PFC_BLOCK_BYTES / rw` so `rw * block_recs` can never exceed the fixed
  `w->xform[PFC_BLOCK_BYTES]` scratch buffer it's transposed through — but
  `pfc_columnar_decode` reads `block_recs` straight from the untrusted stream header with **no
  upper-bound check at all**. A crafted header (`rw=2, cnt=block_recs=40000` → `block_bytes =
  80000 > 65536`) writes past the buffer. Checked `pfc_seq_decode` for the same class of bug —
  it's safe, because it writes directly into the caller's already-capacity-checked `dst` and never
  uses the shared `xform` scratch buffer at all; `pfc_columnar_decode` is the only codec that
  transposes through `xform`, so it's the only one exposed. **Fixed:** added
  `if (block_bytes > PFC_BLOCK_BYTES) return PFC_E_CORRUPT;` right after computing `block_bytes`
  each iteration, before any block-record read is even attempted. Added a regression test
  (`test_columnar_oversized_block`) — a pure 20-byte crafted header, no payload needed, since the
  new check fires before the block-record path is reached.

**Confirmed on the following runs: all four jobs green except `misra`.** Two real bugs found and
fixed by `libfuzzer` in its very first runs — both header-size/header-value validation gaps, in
the newest codec (SPECTRAL) and the most structurally distinct existing one (COLUMNAR, the only
codec using the `xform` scratch buffer). `native` and `bigendian` confirmed fully green after a
test-setup bug in my own regression test was found and fixed (wrong `cap` value — the underlying
fix was already correct). `misra` remains red **by design** until the findings below are
addressed in the codebase — see the MISRA triage section further down.

### CBMC proof: pfc_block_read, and a THIRD real bug found writing it

Authoring the `cbmc` job's harness (`proofs/cbmc/harness_block_read.c`) surfaced a real bug before
the proof ever ran in CI — reading `pfc_block_read`'s bounds check with "does this hold for any
`size_t` width" in mind, not just "does this pass on this 64-bit host", made it visible by hand:

```c
if ((p + PFC_BLKHDR + n) > len) { return PFC_E_CORRUPT; }
```

`n` is the raw 4-byte `payload_len` field, read straight off the wire via `pfc_get_u32` —
attacker/corruption-controlled, up to `UINT32_MAX`. On the 64-bit host every other CI job runs
on, `p + PFC_BLKHDR + n` never overflows `size_t`, so this check has always behaved correctly in
every test this project has ever run — `native`, `libfuzzer` (170k Python iterations, then real
coverage-guided libFuzzer), `bigendian` (`qemu-ppc` is still a 64-bit host process emulating a
32-bit *instruction set*, not a 32-bit `size_t` C runtime). But the actual flight targets
(RAD750, LEON/SPARC, RISC-V-32) are 32-bit machines where `size_t` really is `uint32_t`. There, a
crafted `payload_len` near `UINT32_MAX` makes `p + PFC_BLKHDR + n` wrap around 2^32 to a small
value, satisfies `<= len`, and the function proceeds to treat a multi-gigabyte `n` as a valid
in-bounds payload length — `pfc_crc32(*payload, n)` and whatever codec consumes `*payload`/`*plen`
next would read far past the actual buffer. Every codec's decoder funnels through this one
function (`pfc_spectral.c`, `pfc_seq.c`, `pfc_image.c`, `pfc_columnar.c` all call it directly), so
this was a single point of exposure for the whole decoder on 32-bit flight hardware specifically —
and specifically the class of bug no gate in this pipeline could have found: it's invisible under
every sanitizer, every fuzz run, and every functional test executed so far, because they all run
on 64-bit hosts. This is the textbook case for formal methods over testing: the property depends
on a machine model (32-bit `size_t`) that nothing else in this CI pipeline exercises.

**Fixed** in `pfc_block_read` (and, defensively, the symmetric `pfc_block_write`, even though its
inputs are locally-trusted encoder-side values, not downlink data): rewrote the bounds check to
subtract already-validated quantities instead of summing untrusted-scale ones —

```c
if ((p > len) || ((len - p) < PFC_BLKHDR)) { return PFC_E_CORRUPT; }
rem = (len - p) - PFC_BLKHDR;
if ((size_t)n > rem) { return PFC_E_CORRUPT; }
```

— which cannot overflow on any `size_t` width: `rem` is a subtraction of two quantities already
proven `len - p >= PFC_BLKHDR`, and the final `*pos = p + PFC_BLKHDR + n` is only reached once `n
<= rem` is proven, which bounds that sum by `len` itself. Added a host-level regression test
(`test_block_read_bounds` in `test_pfc.c`) locking in the exact boundary (`n == rem` accepted, `n
== rem + 1` rejected) — though that test can only prove the boundary is correct under the host's
own 64-bit `size_t`; it cannot exercise the wraparound itself. **That's what the CBMC proof is
for**: `harness_block_read.c` run with `--32` (`make cbmc` / the `cbmc` CI job) models a real
32-bit `size_t` and asserts the function's full documented contract — on `PFC_OK`, `payload`/`plen`
lie entirely within `src[0..len)` and `pos` advances by exactly the block size; on rejection,
`pos` is left unchanged. Proved on the fixed code; the old (pre-fix) code would fail this proof
under `--32` with `--unsigned-overflow-check`, which is exactly the point.

**`cbmc` job — first real run (#25) failed, but at the environment step, not the proof.**
`apt-get install cbmc` doesn't work on this runner: Debian bookworm's default repos don't carry a
`cbmc` package at all (`E: Unable to locate package cbmc`) — unlike `cppcheck`/`clang`, this one
just isn't packaged for Debian stable. **Fixed:** install the upstream release `.deb` directly
(`ubuntu-22.04-cbmc-6.10.0-Linux.deb` from `github.com/diffblue/cbmc/releases`, glibc-compatible
with this bookworm container) via `dpkg -i` + `apt-get install -f` to resolve dependencies,
pinned to a specific version rather than "latest" for reproducibility. Whether the proof itself
(against the now-fixed `pfc_block_read`) actually passes once the tool installs correctly is not
yet confirmed as of this writing — that's the next thing to check once this fix lands.

## Other open items

- **Residual −1.3% vs JPEG-LS** on photon-noisy imagery (−0.5% at band 64) — finer context modelling
  that only pays off with bigger bands (memory/containment tradeoff); a directional entropy context regressed.
