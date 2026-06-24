# pertype-flight (`libpfc`)

A lossless, **flight-deployable** compression core derived from pertype — built for the asymmetric
spacecraft→ground link: the **encoder runs on the probe** (radiation-hardened CPU, no operating
system guarantees, no dynamic memory), the **decoder runs on the ground**, and the compressed
stream is byte-identical across both.

> Status: **phases 1–3 built and passing.** Four codecs (image / 1-D seq / float / columnar) on a
> shared integer range coder + block framing; an independent pure-Python ground decoder validated
> byte-for-byte against the C encoder; CCSDS-121 comparison; 20 000-iteration decoder fuzz. The
> remaining items are toolchain-gated (big-endian run, MISRA/libFuzzer reports) — see
> `docs/requirements.md`.

## Why a separate core

`pertype` (the main library) is Python + Rust and uses dynamic memory — fine on the ground, a
non-starter on flight hardware. `libpfc` is the freestanding C99 subset that *can* fly:

- **No dynamic allocation** — never calls `malloc`; all working memory is one caller-supplied
  `pfc_ctx` of compile-time-known size (262 960 B at defaults; retune with `-DPFC_MAX_COLS` /
  `-DPFC_BAND_ROWS`). The compiled library imports **zero** alloc symbols.
- **Integer-only & deterministic** — no floating point; canonical little-endian wire format, so a
  big-endian RAD750 encoder and a little-endian RISC-V/LEON/x86 ground decoder interoperate.
- **Error containment** — every block is independently decodable and CRC-32 protected. A corrupted
  or truncated downlink frame loses exactly one block, is reported, and never reads out of bounds
  (verified under AddressSanitizer/UBSan).
- **No expansion** — `pfc_bound()` is a hard ceiling; incompressible bands fall back to store-raw.
- **Lossless** — bit-exact round-trip, verified on synthetic and real 16-bit instrument imagery.

## Build & test

```sh
make            # build + run the host test suite (70 checks: image/seq/float/columnar)
make strict     # same, warnings-as-errors (-Werror -Wconversion ...)  — MISRA/JPL discipline gate
make asan       # AddressSanitizer + UndefinedBehaviorSanitizer
make crosscheck # R7: C-encode -> independent pure-Python decode == original (incl. real data)
make fuzz       # R6: 20k random/mutated streams through the C decoder, no crash/OOB
make check      # full local gate: strict + asan + crosscheck + fuzz
make misra      # cppcheck MISRA-C:2012 gate (CI; needs cppcheck)
make sharedlib  # build/libpfc.so for the host bridges
python3 test/bench_real.py img.tif    # flight core on real 16-bit data vs JPEG-LS/JPEG-XL
python3 test/ccsds_compare.py img.tif # vs CCSDS-121-class Rice (the flight lossless standard)
```

Flight builds cross-compile the same `src/` with the target toolchain — e.g.
`powerpc-linux-gnu-gcc` (big-endian, RAD750-class), `sparc-gaisler-elf-gcc` (LEON), or a RISC-V
toolchain (HPSC). The code is plain C99 with only `<stddef.h>`/`<stdint.h>`.

## API (see `include/pfc.h`)

```c
size_t     pfc_bound(pfc_codec, size_t n_in);                 /* worst-case output size */
pfc_status pfc_encode(pfc_codec, const pfc_params*, const void* src, size_t n,
                      void* dst, size_t cap, size_t* out, pfc_ctx* work);
pfc_status pfc_decode(const void* src, size_t n, void* dst, size_t cap,
                      size_t* out, pfc_ctx* work);
```

The caller owns every buffer, including the `pfc_ctx` working memory — allocate it once, statically.

## Performance

On real CyCIF 16-bit microscopy the image codec is **1.76× lossless, +2.8% better than the
CCSDS-121 flight standard** (block-adaptive Rice on the same MED predictor) and within **−1.3% of
JPEG-LS** at the default 16-row band — **−0.5% at a 64-row band**. Three levers got here from the
original −2.7%: LOCO-I bias correction (decoupled directional context), adaptive modelling of the
top mantissa bit (residuals within a magnitude bin aren't uniform — a general win across *all*
codecs), and band size (a measured memory/error-containment tradeoff; see `docs/requirements.md`).
A directional *entropy* context was tried and **regressed** on photon-noisy data, so it was dropped.

All with a tiny freestanding core that also gives bounded memory, error containment, and
independent-decoder verification (which the JPEG libraries do not). On smooth gradients ~166×; a
JPEG-LS-style **run mode** reaches **38–49×** on flat-scene data (star fields, masks, label maps —
common in space), a no-op on noisy imagery. The 1-D, float, and columnar codecs reach **261×, 2.0×,
and 14×** on their fixtures (the mantissa-bit model is especially strong on prediction residuals).

## Licensing

`flight/` is **Apache-2.0** (permissive — deployable by government/NASA), separate from the AGPL-3.0
main `pertype` library. The copyright holder relicenses their own algorithms for the flight core.
