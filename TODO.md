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

- [x] Temporal **frame-delta** transform + test harness (`scripts/video_benchmark.py`,
      `.y4m` parsed with numpy, luma). **Hypothesis confirmed** on standard clips
      (60 frames, round-trip verified), vs per-frame JPEG-XL lossless (a stronger
      intra baseline than FFV1): akiyo static temporal **+52%**; foreman −16%,
      stefan −18% (motion). Our `ctxcoder` is the best residual back-end. See
      README "Lossless video". Frame-delta wins static, loses motion — exactly the
      boundary needing motion compensation.
- [ ] 2D spatial predictor (MED / Paeth) for intra frames (shared with images) —
      would help the *intra* side on all clips (and is needed for the motion case
      where temporal delta loses).
- [x] **block motion compensation** prototyped (`scripts/video_mc_benchmark.py`):
      16×16 blocks, ±8 SAD search of the previous frame, (MV + residual) coded by
      `ctxcoder`. Converts the frame-delta motion losses into wins/ties vs
      intra-only JXL (60 frames, round-trip verified): akiyo +52%→+55%, foreman
      −16%→**+3%**, stefan −18%→**−1%**. A ±16 search barely changed it (residual
      cost dominates). Same block-search idea as the LZ match-finder.
- [x] **per-block intra/inter mode selection + MED intra**
      (`scripts/video_mode_benchmark.py`): each block picks INTER (MC residual) or
      INTRA (causal **MED/LOCO-I** predictor, JPEG-LS); mode bit + inter-only MVs +
      residual all ctxcoder-coded; intra pixels reconstructed causally (sentinel
      init → real causal-chain check), verified bit-exact. **Every clip now beats
      intra-only JXL**, including high motion: foreman −16%→**+5%**, stefan
      −18%→**+2%**, akiyo +55%; 27–41% of motion-clip blocks choose intra. Full
      arc: temporal-delta → MC → mode selection → MED.
- [x] **half-pixel motion vectors** (`scripts/video_subpel_benchmark.py`): after the
      integer search, refine each block over the 9 half-pel positions (bilinear
      interpolation), code MVs in half-pel units. Adds +1–4% over integer MVs and
      improves inter enough that fewer blocks fall back to intra. 60 frames vs
      intra-only JXL: akiyo +56%, foreman +5%→**+9%**, stefan +2%→**+6%**.
      Round-trip verified. Full arc takes stefan −18%→+6%, foreman −16%→+9%.
- [x] **per-block SKIP mode** (`scripts/video_skip_benchmark.py`): a block bit-
      identical to its co-located previous block (MV 0) is coded as just a mode
      flag — no MV, no residual. akiyo +2.7% (56% of blocks skip → **+57%** vs
      intra); foreman/stefan unchanged (0% skip — real-camera noise has no exact
      static blocks). Targeted win for screen content / surveillance / animation,
      harmless on noisy video. Round-trip verified.
- [x] **quarter-pixel motion vectors** (`scripts/video_qpel_benchmark.py`): sub-pel
      predictor generalised to one bilinear sampler in quarter-pel units; refine
      integer → half → quarter. Adds +1.5–2% over half-pel: akiyo +58%, foreman
      +10%, stefan +7% vs intra-only JXL. Diminishing returns after half-pel.
      Round-trip verified. Finished arc: stefan −18%→+7%, foreman −16%→+10%.
- [x] **colour planes (U/V)** (`scripts/video_color_benchmark.py`): full pipeline
      run per plane on the 4:2:0 chroma, 60 frames, round-trip verified. Full-YUV
      totals beat intra-only JXL on every clip — akiyo +56% (7.15x vs raw),
      foreman +9%, stefan +5%. But chroma per-plane only wins on static content;
      on motion it's a wash/slight loss (stefan U/V −2–4%) because an *independent*
      chroma motion search spends MV+mode bits that don't pay on smooth low-energy
      planes.
- [x] **derive chroma MVs from luma** (`scripts/video_joint_benchmark.py`): tested
      the textbook joint design — one mode + one luma MV per block, chroma inherits
      a scaled MV, no chroma MV/mode coded. **Slightly worse** than independent
      per-plane (akiyo −2.7%, foreman −0.2%, stefan −0.5%, 60 frames, round-trip
      verified): it gives up per-plane SKIP (chroma static while luma moves) and a
      plane-optimal mode, while `ctxcoder` already codes chroma MVs/modes so cheaply
      that the saved overhead is negligible. Kept the independent coder. Lesson:
      the shared-MV design only pays when MV/mode coding is expensive.
- [x] **first-class video codec** (`compressor/videocodec.py`): the validated
      pipeline is now a real `encode`/`decode` (+ `encode_yuv`/`decode_yuv`) with a
      VID1 container, not just benchmark scripts — quarter-pel MC + per-block
      SKIP/INTER/INTRA (MED), residuals/MVs via `ctxcoder`, numpy+ctxcoder only.
      Round-trip tests added (all modes / single-frame / static / YUV; 78 tests
      pass) and verified on real clips (akiyo 6.58x, foreman 2.30x vs raw luma,
      bit-exact). Decode's MED loop only touches intra pixels (fast).
- [x] **video via the CLI** (`video-encode` / `video-decode` in `cli.py`): operate
      on `.y4m`; the container stores the y4m header so decode reproduces the file
      byte-exact (verified on akiyo: 6.73x, `cmp`-identical). Now handles
      **4:2:0 / 4:2:2 / 4:4:4 / mono** and preserves arbitrary per-frame headers
      verbatim (tests for each). Subject to the codec's plane-dims-multiple-of-16
      requirement.
- [x] **real FFV1 baseline** (`scripts/video_ffv1_benchmark.py`): static ffmpeg via
      the `imageio-ffmpeg` wheel (no system install). Full YUV, 60 frames,
      round-trip verified — **we beat FFV1 on every clip**: akiyo +53%, foreman +8%,
      stefan +8% (FFV1 is intra-only; we win via motion compensation). JXL-intra was
      within ~3% of FFV1, confirming it was a fair stand-in.
- [x] **native MED reconstruction loop** (`med_fill` in `_native/audio.c`):
      byte-identical to the Python loop; decode ~2.6× on motion clips (more on
      intra-heavy frames). videocodec.decode dispatches to it.
- [x] **consolidated / deduped the video experiments**: the 8 exploratory
      `scripts/video_*_benchmark.py` ablation scripts (~1400 lines, heavily
      duplicated) are retired to git history now that the pipeline lives in the
      tested `compressor/videocodec.py`; `scripts/video_ffv1_benchmark.py` (ours vs
      FFV1/JXL via the real codec) is the one remaining, canonical video benchmark.
      The completed video items above name those now-retired scripts as the
      historical site of each ablation.
- [ ] **NEXT for video**: SKIP against the best MC MV (not just MV 0); more clips
      across the motion spectrum.

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
- [x] **Sensor telemetry (UCI household power)** — first reported as a loss
      (delta+**Rice** 2.78x vs xz 8.56x) with the wrong remedy ("needs LZ").
      **Corrected**: delta + **ctxcoder** (order-2, never tried here originally) gets
      **6.27x** — beats gzip (6.15x), within ~1.4x of xz. The order-2 context coder
      handles the long zero-runs (after a zero the conditioned bucket→0 prob ≈ 1, so
      ~0 bits/zero; the 95%-zero column goes 4.96x→83x). No LZ needed.
      `scripts/scidata_ctx_benchmark.py`. Same `delta+ctxcoder` wins on ECG too.

Still high-value untested, in rough priority:

- [x] **Floating-point data** (`scripts/float_benchmark.py`) — tested; a genuine
      boundary. Integer transforms *hurt* float bytes (split8 2.0x vs raw+xz 6.16x
      on measurement float64); a Gorilla XOR-delta helps only marginally and only on
      smooth data (1.36x vs 1.28x), since float64 mantissas are high-entropy
      (smooth float ~1.3x, near-incompressible). "Fixed-precision → int" isn't
      lossless (4.216 has no exact float64). XOR-delta measured and **not added**
      (marginal; `split` proxy-selection already adapts). Real FP compression needs
      FCM/DFCM value prediction + leading-zero/Gorilla coding — a separate build,
      low priority. Raw bytes + general coder is the pragmatic best.
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
- [x] **beat xz where prediction wins** — direct head-to-head: **audio** ours 1.96×
      vs xz 1.24× (+59%, 8/8 PCM tracks); **ECG** 3.06× vs 2.94×. On LZ-friendly
      *repetitive* numeric (UCI power) xz wins (8.55× vs our 6.27×) and tried
      approaches (per-column predictor selection, zero-run-length + ctx) don't close
      it — xz codes long runs as one LZ match where our coder pays per symbol;
      matching it would mean reimplementing LZMA's LZ + range coder. Honest boundary:
      we beat xz on prediction-friendly signals, xz beats us on repetitive data.
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
