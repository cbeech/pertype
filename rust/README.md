# pertype — Rust port

A Rust port of the project's compression core — **byte-identical** to the Python reference
(`pertype/`) and its C twin (`pertype/_native/audio.c`), so a blob produced by any of
the three decodes in the others. Built as a `cdylib` behind the same C ABI as the C native,
a drop-in for the ctypes loader. Modules:

- **`arith`** — the Witten–Neal–Cleary 32-bit arithmetic coder + MSB-first bit I/O (shared).
- **`ctxcoder`** — the context-adaptive residual coder every numeric/image/columnar/float
  codec runs through (order-2 magnitude bucket + top-mantissa-bit model per `(ctx, k)`).
- **`calic`** — the full CALIC image codec (GAP prediction + 704-context bias correction +
  energy-conditional coding) — the continuous-tone workhorse (photos, raw, DEM, medical,
  FITS, hyperspectral).
- **`columnar`** — a *complete standalone codec* for fixed-width binary record streams
  (de-interleave → per-column raw/delta/Δ² → `COL1` container with store fallback). Wins on
  LiDAR-style point data; produces the same container bytes as the Python version.
- **`floatcodec`** — low-cardinality float codec (value-dictionary + delta-coded indices →
  `FLT1`). Wins on fixed-precision scientific floats (weather/climate grids).
- **`csvcolumnar`** — delimited-text table codec (grid detect → transpose → per-column
  numeric / text-dictionary / deflate → `CSV1`). Wins on numeric CSV.
- **`auto`** — front door: tries the codecs above + deflate + store, verifies, keeps the
  smallest, and emits the same **`AZ` container as Python's `auto`** (methods store / deflate
  / csv / columnar) — so a Rust `.az` is decoded by Python's `auto_decompress` and vice versa.
- **`transform`** — the reversible byte transforms (`delta`, `split`), byte-identical to
  Python; the building blocks the per-type model selects.
- **`imagecodec`** + **`predictors`** — the full image codec: per-plane MED / CALIC / RLE
  selection, gray / Bayer / RGB modes, and inter-slice-delta volumes (`RIMG` / `RVOL`),
  byte-identical to Python (gray/RGB/int16/Bayer/uint16-volume all verified).
- **`audiocodec`** — the lossless audio codec: mid/side → fixed order-2 → 3-stage sign-sign
  LMS cascade → adaptive Rice or `ctxcoder` (`AUD1`). Byte-identical to Python/C for both
  back-ends (integer cascade uses wrapping arithmetic to match the C `-fwrapv`; the Rice
  run-magnitude uses `f64` exactly as the reference).
- **`videocodec`** — the lossless video codec: per-16×16-block SKIP / INTER (quarter-pel
  motion-compensated) / INTRA (MED) selection, hierarchical motion search, `ctxcoder`-coded
  mode/MV/residual streams, independent-plane YUV (`VID1` / `VYUV`). Byte-identical to Python
  (every numpy motion-search tie-break reproduced — strict `<`, zero-MV preference on ties).
- **`textcodec`** — the trained per-file-type text/byte codec: loads a Python-trained model
  (`CMP7`) and runs the full pipeline — reversible transform → cost-optimal LZ + dictionary
  parse → Witten–Neal–Cleary arithmetic coding of the token stream → `CZ` container. The
  decode path is pure integer arithmetic; the cost-optimal parse prices tokens with the same
  `f64` `log2` costs as the reference, so the **whole codec is byte-identical** — including
  the float-priced optimal parse, repeat-offset cache, and all four transforms. It also
  **trains its own models** (`train_model`): dictionary mining, COVER blob building, the
  greedy + cost-optimal parse, the blob-strategy search and freq-table quantization — all
  byte-identical to `model.py`, *except* the transform selector's `zlib` proxy (flate2 ≠
  CPython zlib, so on borderline numeric data it can pick a different — still valid and
  cross-loadable — transform). So Rust can now build models end-to-end with no Python.

**Guarantee.** Every codec except the two `zlib`-using ones is **byte-identical** to
Python/C (`ctxcoder`, `calic`, `columnar`, `transform`, `imagecodec`, `audiocodec`,
`videocodec`, `textcodec`). `floatcodec` and `csvcolumnar` additionally use `zlib`
(dictionary / text columns); since Rust's deflate differs from CPython's, those sub-blobs
are *not* byte-identical, but the streams are valid and **cross-decodable both directions**
(Python decodes Rust's output and vice versa) at the same ratio — so the codecs are fully
interoperable and lossless. All verified in `tests/test_rust_port.py`.

**Parallelism.** The columnar and CSV codecs encode their independent columns with `rayon`
(order-preserving, so the output bytes are unchanged) — e.g. the 34-field LiDAR record
encodes ~4.5× faster across cores than single-threaded, byte-identical.

The port is now **feature-complete**, compress *and train*: every codec in `pertype/` has
a byte-identical (or, for the two `zlib`-using codecs, cross-decodable) Rust twin behind the
same C ABI, and the trained text codec can build its own models without Python (byte-identical
bar the `zlib` transform-proxy seam).

## Standalone CLIs (no Python)

```bash
cargo build --release

# pertype: the unified tool — mirrors the Python `pertype` command, fully no-Python.
target/release/pertype train json corpus/json/train -o json.model   # trains its own model
target/release/pertype compress page.json -m json.model             # -> page.json.cmp (8.4x)
target/release/pertype decompress page.json.cmp -m json.model       # -> page.json
target/release/pertype compress data.csv                            # no model -> auto-routes

# azc / colz: the auto-router and columnar codec directly
target/release/azc  enc data.csv data.az          # e.g. power CSV -> 14.8x [csv->columnar]
target/release/colz enc points.bin points.col     # auto-detects the record period (LiDAR 4.38x)
```

Everything is **interchangeable with the Python tool**: the Rust `pertype`'s `.cmp` output
(auto *or* trained-model) decompresses byte-exact in Python and vice versa, and a Rust-trained
model is byte-identical to a Python-trained one — verified end-to-end. (`azc`'s `.az` matches
Python `auto`; `colz`'s `.col` matches Python `columnar`.)

## Build & verify

```bash
cd rust
cargo test --release          # Rust round-trip unit test
cargo build --release         # builds target/release/deps/libpertype.so
```

Then from the repo root, the Python parity test picks the cdylib up automatically:

```bash
python3 -m pytest tests/test_rust_port.py   # byte-identical + cross-compatible vs Python/C
```

## Status

- **Correctness:** the full codec set is byte-identical to Python/C (the two `zlib` codecs
  cross-decodable both directions), verified on real data (LiDAR, Kodak, sao) and synthetic
  audio/video/text fixtures in `tests/test_rust_port.py`.
- **Speed:** ctxcoder ~3.9 M residuals/s encode (≈ the C native's order; **~32× faster than
  pure Python**), memory-safe.
- **C ABI:** `ctx_encode`/`ctx_decode`, `calic_codec_encode`/`calic_codec_decode`,
  `columnar_encode`/`columnar_decode`, `float_encode`/`float_decode`, `csv_encode`/`csv_decode`,
  `image_encode`/`image_decode`, `volume_encode`/`volume_decode`, `audio_encode`/`audio_decode`,
  `video_encode`/`video_decode`, `text_compress`/`text_decompress`, `train_model`, plus the
  `transform_*` ops and `auto_encode`/`auto_decode` — all drop-ins for the ctypes loader.

## Benchmarks

`scripts/rust_vs_python_benchmark.py` times each codec, Rust cdylib vs Python. Since the
port is byte-identical the **ratios are equal** — this is pure throughput. Crucially the
"Python" side is the *fast* path: Python orchestration over the **C native** inner loops
(`HAVE_NATIVE=True`), so this is effectively **Rust vs C**, not Rust vs pure-Python (which
would be ~30–100×). MB/s on the uncompressed input; `x` = Rust speedup (Python 3.13, 1 host):

```
codec                                MB   enc x   dec x
ctxcoder (2M residuals)           16.00    0.8x    1.6x
CALIC image (512x512)              0.26    0.8x    0.9x
columnar (16-field, 80k recs)      1.28    2.9x    1.6x
floatcodec (300k f32)              1.20    0.8x    1.6x
csvcolumnar (60k rows)             1.34    2.8x    2.8x
imagecodec (384x384 RGB)           0.44    2.3x    1.5x
audiocodec (200k stereo, rice)     0.80    1.1x    1.1x
videocodec (12x144x176, QCIF)      0.30    4.9x    2.2x
textcodec (~50KB, LZ-optimal)      0.03    2.3x    9.8x
```

**Decode is faster across the board** (1.1–9.8×) — Rust collapses Python's per-token / per-row
overhead. **Encode** splits three ways: the rayon-parallel codecs (columnar, csv, image,
video) win 2.3–4.9×; the pure inner-loop codecs (ctxcoder, CALIC, audio, float) sit at
par (0.8–1.1×) because Python already runs *those exact loops* in C; and the textcodec's
cost-optimal parse — once tuned (Fibonacci-hashed match-finder, word-wise `match_len`,
allocation-free candidate dedup) — now beats the hand-tuned C native at **2.3× encode**.

Run it:

```bash
(cd rust && cargo build --release)
PYTHONPATH=. python3 scripts/rust_vs_python_benchmark.py
```

### Training (`scripts/rust_vs_python_train_benchmark.py`)

Building a model is the heaviest path. Here the Python side is a *mix* — pure-Python mining /
blob building + a C-native parse — and the Rust trainer is all-native (single-threaded). Both
serial (fit slices < 512 KB, so Python's process pool doesn't fork); wall-clock seconds:

```
corpus         samples      KB use_lz   py s   rust s  speedup
json               400      22   True  163.1     1.4    115x
logs               600      36   True  272.6     2.5    109x
http              1200     177   True  460.6    18.1     25x
float64             40      94   True   36.5     3.1     12x
```

**11–115× faster.** The win is largest where pure-Python dictionary mining dominates (small
text → 100×+) and smallest where the C-native parse does (float64 → 12×). Output is
byte-identical wherever the `zlib` transform proxy agrees. Past a 512 KB fit slice Python's
process pool narrows the gap on the blob search; rayon over that search is the matching Rust
follow-up (the trainer is currently serial).

## Why Rust here

A Rust port is a **performance / distribution** step, not a compression one — the ratios
are already validated in Python (see the repo `README.md` and `docs/`). It matters when the
goal shifts from research to shipping a fast, dependency-light library.
