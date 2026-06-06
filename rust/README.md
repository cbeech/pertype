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

All four are verified byte-identical and cross-compatible (both directions) in
`tests/test_rust_port.py`. The remaining pieces toward a fully standalone library are the
other front-ends (CSV/float), the MED/transform loops, the detect/auto router, and `rayon`
block parallelism.

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
  `columnar_encode`/`columnar_decode` — drop-ins for the ctypes loader.

## Why Rust here

A Rust port is a **performance / distribution** step, not a compression one — the ratios
are already validated in Python (see the repo `README.md` and `docs/`). It matters when the
goal shifts from research to shipping a fast, dependency-light library.
