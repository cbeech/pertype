# compressor_rs — Rust port (hot path)

A Rust port of the project's **context-adaptive arithmetic residual coder** — the entropy
back-end every numeric / image / columnar / float codec runs through. It is a faithful,
**byte-identical** twin of `compressor/ctxcoder.py` and its C version in
`compressor/_native/audio.c`: same Witten–Neal–Cleary 32-bit arithmetic coder, same
per-context magnitude-bucket model with the top mantissa bit modelled per `(context, k)`
and the rest raw, MSB-first with a zero-padded final byte. A blob encoded by Python or C
decodes here and vice versa — verified in `tests/test_rust_port.py`.

This is the **first piece** of an eventual standalone crate. The C native already covers
the hot loops via ctypes; this proves the same path in safe Rust and is the natural base
for a no-Python library/CLI (the predictors, columnar/CSV/float front-ends, and `rayon`
block parallelism would follow).

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

- **Correctness:** byte-identical to Python/C, cross-compatible both directions.
- **Speed:** ~3.9 M residuals/s encode (≈ the C native's order; **~32× faster than pure
  Python**), memory-safe.
- **API:** C ABI `ctx_encode(res, n, out, cap) -> bytes` / `ctx_decode(in, len, n, out)`,
  a drop-in for the ctypes loader.

## Why Rust here

A Rust port is a **performance / distribution** step, not a compression one — the ratios
are already validated in Python (see the repo `README.md` and `docs/`). It matters when the
goal shifts from research to shipping a fast, dependency-light library.
