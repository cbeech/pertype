# compressor_rs — Rust port

A Rust port of the project's compression core — **byte-identical** to the Python reference
(`compressor/`) and its C twin (`compressor/_native/audio.c`), so a blob produced by any of
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

**Guarantee.** The pure-arithmetic codecs (`ctxcoder`, `calic`, `columnar`) are
**byte-identical** to Python/C. `floatcodec` and `csvcolumnar` additionally use `zlib`
(dictionary / text columns); since Rust's deflate differs from CPython's, those sub-blobs
are *not* byte-identical, but the streams are valid and **cross-decodable both directions**
(Python decodes Rust's output and vice versa) at the same ratio — so the codecs are fully
interoperable and lossless. All verified in `tests/test_rust_port.py`.

**Parallelism.** The columnar and CSV codecs encode their independent columns with `rayon`
(order-preserving, so the output bytes are unchanged) — e.g. the 34-field LiDAR record
encodes ~4.5× faster across cores than single-threaded, byte-identical.

Remaining toward a fully standalone library: the MED/transform loops and the detect/auto
router.

## Standalone CLI (`colz`)

A no-Python command-line tool for the columnar record codec (output interchangeable with
the Python codec):

```bash
cargo build --release
target/release/colz enc points.bin points.col   # auto-detects the record period
target/release/colz dec points.col points.out   # byte-exact round-trip
```

(End-to-end on real LiDAR point data: 4.38×, and the Python codec decodes the result.)

## Build & verify

```bash
cd rust
cargo test --release          # Rust round-trip unit test
cargo build --release         # builds target/release/deps/libcompressor_rs.so
```

Then from the repo root, the Python parity test picks the cdylib up automatically:

```bash
python3 -m pytest tests/test_rust_port.py   # byte-identical + cross-compatible vs Python/C
```

## Status

- **Correctness:** all four modules byte-identical to Python/C, cross-compatible both
  directions, on real data (LiDAR, Kodak, sao).
- **Speed:** ctxcoder ~3.9 M residuals/s encode (≈ the C native's order; **~32× faster than
  pure Python**), memory-safe.
- **C ABI:** `ctx_encode`/`ctx_decode`, `calic_codec_encode`/`calic_codec_decode`,
  `columnar_encode`/`columnar_decode`, `float_encode`/`float_decode`,
  `csv_encode`/`csv_decode` — drop-ins for the ctypes loader.

## Why Rust here

A Rust port is a **performance / distribution** step, not a compression one — the ratios
are already validated in Python (see the repo `README.md` and `docs/`). It matters when the
goal shifts from research to shipping a fast, dependency-light library.
