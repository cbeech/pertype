# TODO / Roadmap

Future work, captured. Current state: a per-type trained text/byte compressor + a
reversible transform stage + a dedicated adaptive-filter audio codec — validated
across text (≈parity with `zstd --train`), raw images (parity with JPEG XL), and
audio (beats FLAC). See `README.md` for results. Everything below is *future*.

---

## 1. Optimised port — IN PROGRESS

The algorithms are validated; pure Python is the only blocker. Approach
established: C primitives in `compressor/_native/`, compiled by gcc on import,
called via ctypes (`compressor/native.py`), bit-identical to the pure-Python
reference, with a Python fallback and a zero-dependency core (lazy native import).

**Port the reusable PRIMITIVES, not the pipelines** — keep orchestration + the
validation gate + new-domain prototyping in Python (numpy / PyTorch model).

Primitives:
- [x] adaptive sign-sign LMS filter (audio) — ~25×
- [x] fixed-2 predictor + adaptive Rice coder (audio) — audio codec now ~12 s
      audio in ~0.4 s each way (was minutes)
- [x] `delta` transform (arbitrary stride) — ~133×
- [x] **LZ match-finder / cost-optimal parse forward pass** — done (`lz_forward`).
      The 3-byte hash-chain search + `_match_len` (61% of the parse) is in C,
      producing integer-exact candidate lists so the Python DP is unchanged and
      tokens are identical. `compress` on 0.8 MB text: 111 s → 7.6 s (~15×). The
      basis for video motion search.
- [x] **greedy match-finder + dict matcher** — `lz_best` (greedy single-best per
      position) and `dict_match_all` (trained-dictionary longest-match per
      position) in C; the greedy/lazy walk and the optimal DP read the resulting
      integer-exact arrays (identical tokens, all 3 parse paths verified). compress
      7.6 s → 2.9 s, train 103 s → 67 s. The numeric `use_lz=False` path is native too.
- [x] **cost-optimal backward DP (`lz_dp`)** — done. The DP runs in C on a
      match-cost lookup table built by probing the cost callables (no model
      access); double arithmetic is bit-identical, tokens match. **The entire
      compress/decompress hot path is now native** — `compress` of 0.8 MB text
      111 s → 0.78 s (~140×). Remaining pure-Python is *training*-only (pattern
      mining + blob building), not the parse.
- [ ] (optional) port **pattern mining / blob building** — the last Python in
      training (~54 s of it); not on the compress path, so lower priority.
- [x] **context-adaptive arithmetic coder (`ctxcoder`)** — ported, byte-identical
      both directions; ~45–60× (ECG record 12.6 s → 0.28 s). The data where we
      *beat xz* is now fast.
- [x] arithmetic / range coder for the **text/LZ codec's** bit loop — done.
      `codec.py`'s whole per-symbol token loop (main/dist/mode models +
      repeat-offset cache + slot bits) is in C (`lz_encode`/`lz_decode`),
      byte-identical, enc ~27× / dec ~46×. The entropy half of the 57 MB numeric
      run is now fast.
- [ ] `split` transform (already fast via slicing; low priority)

**Multi-threading / parallelism** (large data splits into independent blocks):
- Blocks/files/channels are already self-contained (per-block headers) →
  parallelism is bit-exact (deterministic; reassemble in order). Demonstrated:
  8 independent audio chunks gave 3.8× via a thread pool, identical output.
- The native (ctypes) primitives **release the GIL**, so Python *threads*
  parallelize the C hot loops today; pure-Python paths need *multiprocessing*.
  A Rust port → `rayon` over blocks = near-linear.
- Make the **block the unit of both seeking and parallelism**. Tradeoff: smaller
  blocks = more parallelism but more adaptive-filter / Rice re-warmup and lost
  cross-block redundancy. Expose block size as the knob (like FLAC frames /
  `zstd -T0`).

Two design rules so the port does **not** ossify (these keep future prototyping
fast rather than hindered):
- **Generic abstractions:** a `Transform` is any reversible `apply`/`invert`; a
  `Coder` is `encode`/`decode`; a `Model` is per-type config. Domain pipelines
  (text / image / audio / video / science) are *compositions* of these — never
  hard-code a specific pipeline into the fast layer.
- **Pure-Python fallback for every primitive:** prototype new transforms/
  predictors in Python, validate the ratio on a proxy, and only push to the fast
  kernel once proven. This preserves the proxy-then-build workflow.

### Future: a full Rust port (not needed now)

The C-via-ctypes primitives already deliver the speed, so this is a longer-term,
optional step — pursue it only when the goal shifts from *research* to *shipping a
real library/CLI*. What a Rust port would buy:

- **Distribution as a single self-contained binary / crate** — no gcc-at-import,
  no Python/numpy runtime needed; usable from other languages.
- **Near-linear multi-threading** via `rayon` over independent blocks (vs the
  GIL-bounded ~3.8× we get from Python threads over ctypes today).
- **Memory safety + maintainability** for the whole pipeline (not just hot loops),
  and SIMD-friendly inner loops.
- Likely **another large speed step** beyond the C primitives (whole-pipeline
  native, no Python/ctypes/numpy boundary crossings per block).

Keep the same architecture: generic `Transform`/`Coder`/`Model` traits, the
per-type validation gate, block = unit of seek + parallelism. A pragmatic path is
to port incrementally behind the existing `native.py` seam (Rust via `cffi`/a C
ABI, same as the current C), so the Python orchestration and prototyping workflow
keep working throughout — then optionally move orchestration into Rust last.
Reuse a reference Rust audio/range-coder crate where sensible rather than
reimplementing from scratch.

---

## 2. Lossless video

Two redundancy axes: **spatial (intra-frame)** + **temporal (inter-frame)**. Most
lossless video codecs (FFV1, Ut Video, MagicYUV) are **intra-only** — they ignore
temporal redundancy, which is usually the dominant source of compressibility.

- [ ] Temporal **frame-delta** transform = `delta` with `stride = bytes-per-frame`
      (reuses the delta primitive + frame-dimension awareness). Hypothesis: beats
      intra-only FFV1 on static / slow content.
- [ ] 2D spatial predictor (MED / Paeth) for intra frames (shared with images).
- [ ] (Hard) block **motion compensation** for moving content — built on the
      match-finder primitive. Where dedicated motion-compensated codecs win.
- [ ] Test harness: decode short clips to raw frames (`imageio` / `pyav`), compare
      vs **FFV1** and per-frame PNG / JPEG-XL on static vs high-motion clips.
      Falsifiable hypothesis: temporal delta beats intra-only FFV1 on static,
      loses on high motion.

---

## 3. Test more data types

Fits structured / numeric data; useless on already-compressed / encrypted / noise.

**Tested (2026-06):** two real datasets, every result round-trip verified — see
README "Scientific numeric time-series". Key finding: **the predictor and the
entropy coder interact** — strong adaptive predictor + Rice ≈ weak predictor +
context-adaptive coder. The new `compressor/ctxcoder.py` (context-adaptive
arithmetic, order-2 context) beats `xz -9` on ECG (3.16x vs 2.94x) where Rice
lost, but does *not* help audio (the LMS cascade already whitens the residual).
- [x] **Biosignals (ECG)** — PhysioNet Apnea-ECG. delta + ctx **beats xz**. The
      audio LMS codec did *not* transfer as-is (its music-tuned params overshoot
      ECG's sharp QRS — 1.38x); plain delta + the context coder is the right tool.
- [x] **Sensor telemetry (UCI household power)** — **lost** (2.90x vs xz 8.56x).
      Repetition-dominated (51 % zero-deltas → long constant runs) is LZ/RLE
      territory; our fast path has no LZ. This needs the native LZ port (§1), not
      a predictor. My "delta will win" prior was wrong for repetitive data.

Still high-value untested, in rough priority:

- [ ] **Floating-point data** — needs a new XOR-delta / float byte-plane primitive
      (Gorilla / FPC style). Boundary test of the transform repertoire.
- [ ] **Seismic / vibration / accelerometer** — genuinely high-rate, low-repetition
      signals: the regime where prediction should beat LZ. Confirms the niche.
- [ ] **Columnar DB numeric columns** — delta / RLE / dictionary (Parquet/ORC).
- [ ] **Scientific / medical arrays** — HDF5, FITS, DICOM 16-bit volumes,
      hyperspectral / satellite (de-interleave bands + delta).
- [ ] **More text formats** — XML, YAML, TOML, CSV, source code, FASTA/FASTQ/VCF.

---

## 4. Algorithm improvements (from the README roadmap)

- [x] **`ctxcoder` order-2 context** — conditioning each residual's magnitude
      bucket on the previous *two* buckets (vs one) lifted ECG 3.06x → 3.16x
      (+3.3%), widening the lead over xz to +7.6%. Chosen by measuring the
      residual's conditional entropy (order-2 4.97 b/s vs order-1 5.14, xz 5.39);
      order-3 and mantissa-bit modelling measured and rejected (too sparse / ~0.7%).
- [ ] Better **dictionary trainer for heterogeneous text** (proper COVER /
      suffix-automaton) — close the remaining gap to `zstd --train` on real text.
- [ ] More **transforms**: 2D predictors, RLE for the zero-runs decorrelation
      produces, channel de-interleaving.
- [x] **Audio: third LMS stage** — added a 512-tap (shift 14) stage after the
      16/256 cascade. Measured on real music: mean ratio 1.90x → 1.92x, FLAC
      advantage +5.9% → +7.4% (better on 11/12 tracks). 512,14 beat a 1024,15
      variant and is cheaper.
- [ ] **Audio**: still longer / per-track-adaptive filter orders; definitive
      comparison vs `flac -8` (needs the `flac` binary, on the unmounted NAS).
- [ ] **Faster training** even before a full port: reuse blob hash chains across
      files; rep-offset-aware cost-optimal parsing.
