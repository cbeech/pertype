# TODO / Roadmap

Future work, captured. Current state: a per-type trained text/byte compressor + a
reversible transform stage + a dedicated adaptive-filter audio codec — validated
across text (≈parity with `zstd --train`), raw images (parity with JPEG XL), and
audio (beats FLAC). See `README.md` for results. Everything below is *future*.

---

## 1. Optimised port — CURRENT PRIORITY

The algorithms are validated; pure Python is the only blocker (full 42 MB raw
frames take hours; the audio codec is slow). Goal: production-grade speed.

**Port the reusable PRIMITIVES, not the pipelines** — a fast Rust crate / C lib
with a thin Python binding, keeping orchestration + the validation gate +
new-domain prototyping in Python (the numpy / PyTorch model: Python glue, native
kernels).

Primitives to port:
- bit I/O; arithmetic / range coder (+ adaptive models); Rice / Golomb coder
- LZ match-finder (hash chains) — also the basis for video motion search
- transforms: `delta` (arbitrary stride), `split` (arbitrary N)
- adaptive sign-sign LMS filter (audio; reusable for biosignals / seismic)

Two design rules so the port does **not** ossify (these keep future prototyping
fast rather than hindered):
- **Generic abstractions:** a `Transform` is any reversible `apply`/`invert`; a
  `Coder` is `encode`/`decode`; a `Model` is per-type config. Domain pipelines
  (text / image / audio / video / science) are *compositions* of these — never
  hard-code a specific pipeline into the fast layer.
- **Pure-Python fallback for every primitive:** prototype new transforms/
  predictors in Python, validate the ratio on a proxy, and only push to the fast
  kernel once proven. This preserves the proxy-then-build workflow.

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
High-value untested, in rough priority:

- [ ] **Time-series / sensor / IoT telemetry** — `delta` (timestamps, monotonic
      IDs, measurements). Expected large win; most commercially relevant.
- [ ] **Columnar DB numeric columns** — delta / RLE / dictionary (same toolkit as
      Parquet/ORC encodings).
- [ ] **Scientific / medical arrays** — HDF5, FITS, DICOM 16-bit volumes,
      hyperspectral / satellite (de-interleave bands + delta).
- [ ] **Floating-point data** — needs a new XOR-delta / float byte-plane primitive
      (Gorilla / FPC style). Boundary test of the transform repertoire.
- [ ] **Biosignals (ECG/EEG), seismic traces** — reuse the audio LMS codec as-is.
- [ ] **More text formats** — XML, YAML, TOML, CSV, source code, FASTA/FASTQ/VCF.

---

## 4. Algorithm improvements (from the README roadmap)

- [ ] Better **dictionary trainer for heterogeneous text** (proper COVER /
      suffix-automaton) — close the remaining gap to `zstd --train` on real text.
- [ ] More **transforms**: 2D predictors, RLE for the zero-runs decorrelation
      produces, channel de-interleaving.
- [ ] **Audio**: longer / multi-stage adaptive filters; definitive comparison vs
      `flac -8` (needs the `flac` binary, on the unmounted NAS).
- [ ] **Faster training** even before a full port: reuse blob hash chains across
      files; rep-offset-aware cost-optimal parsing.
