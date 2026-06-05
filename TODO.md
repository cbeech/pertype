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
- [~] 2D spatial predictor (MED / Paeth) for intra frames (shared with images) —
      built `compressor/predictors.py` (MED + Paeth, vectorised forward + causal
      inverse, round-trip tested incl. odd shapes; `tests/test_predictors.py`).
      Measure-first (`scripts/image_med_benchmark.py`) settled whether to build an
      image-codec path around it, and the answer is **no, on the data we have**:
        * MED/Paeth + ctxcoder ALONE loses badly to PNG/xz on icons (4.4x vs 7-8x)
          and natural wallpaper crops (3.7x vs 7x) — it discards LZ, while those
          images are repetition-heavy and PNG/xz/our-codec exploit that.
        * MED-residual -> our FULL codec beats PNG on icons (5.94x vs 4.98x) BUT our
          generic codec with no prediction is 6.18x — **MED hurts by 4%** there,
          because it breaks the exact cross-image repetition the dictionary matches.
      So 2D intra prediction only pays off on genuinely continuous-tone, noisy data
      with no exact repeats — and on **real Canon CR2 raw it does, decisively**
      (`scripts/cr2_med_benchmark.py`, 67 raws copied locally from the NAS to
      ~/raws). Deinterleaving the RGGB mosaic into same-colour sub-planes,
      **MED + ctxcoder (pure prediction, NO LZ, no trained model) = 1.99x** vs our
      generic codec 1.76x, xz 1.68x, PNG-16 1.28x (held-out 256x256 crops). Routing
      MED residuals through the LZ codec drops to 1.74x — LZ hurts on noise. So:
      build a dedicated **raw-image path** (Bayer-deinterleave -> MED -> ctxcoder, no
      LZ); leave graphics to the LZ+dictionary codec. Predictor module + tests done.
- [x] **Raw-image codec path** — built `compressor/imagecodec.py`: Bayer-deinterleave
      → 2D MED → ctxcoder (no LZ, no model), RIMG container + dims header + CRC, CLI
      `image-encode`/`image-decode` (.npy or .CR2 → .rimg), `tests/test_imagecodec.py`.
      Decode uses the native `med_fill` (predictors aligned to origin 128 so the
      vectorised forward and the C reconstruction are byte-identical) — ~2 s enc /
      ~3 s dec per 21-MP frame. On 10 held-out full-frame Canon raws (423 MB),
      round-trip verified: **ours 2.12×** vs xz 1.81×, Canon .CR2 1.57×, zstd 1.52×,
      PNG-16 1.33× (beats the camera's own lossless +35%).
- [x] **RGB/photo mode** for the image codec — a reversible green-subtract colour
      transform (G, R-G, B-G; +7% over no-RCT, edged out YCoCg-R) then MED per plane;
      RIMG v2 container carries mode (gray/Bayer/RGB) + itemsize (8/16-bit). On 8
      held-out full-frame demosaiced Canon photos (507 MB), round-trip verified:
      **ours 2.57×** vs PNG 2.33×, xz 1.88×, zstd 1.73× — beats PNG +9%, xz +37%.
      CLI `image-encode`/`image-decode` handle 2D (Bayer/gray) and 3D (RGB) .npy.
- [x] **Stronger predictor + per-plane selection + MED unification.**
      * Added a **GAP** (CALIC gradient-adjusted) predictor to `predictors.py`
        (vectorised forward with arithmetic-shift divisions + a native `gap_fill`
        that's byte-identical; pure-Python fallback). Per-plane selection in
        imagecodec (RIMG v3, 1-byte selector/plane; scale from itemsize): each plane
        takes the cheaper of MED or GAP. Measured: GAP wins ~20/24 Bayer sub-planes,
        **full-frame Bayer 2.12 → 2.17× (+2.3%)**; MED stays best on RGB (no
        regression). Paeth measured, never won, dropped from the shipped set (decode
        still honours selector 1, so re-enabling is format-compatible).
      * **Unified videocodec's MED onto `predictors.py`** — `_med_predict` now
        delegates to `predictors.med_predict` (byte-identical: the gradient branch
        never overflows uint8, origin/edges match), removing the duplicate. 12 video
        tests green, byte-output unchanged.
      Honest note: the predictor gain is modest because `ctxcoder`'s order-2 context
      already compensates for predictor choice; GAP only clearly helps the smooth raw
      planes.
- [x] **CALIC-style context bias correction** — added as a 3rd selectable predictor
      (`calic`, code 3). On top of GAP, a running mean prediction error per context
      (energy = dh+dv+2|e_west| quantised to 11 bins × 6 texture sign-bits = 704
      contexts; B[k]/C[k] with 256-halving) is subtracted, removing GAP's systematic
      per-context bias. Native `calic_code` (one sequential function for encode and
      decode, all-integer, byte-exact; pure-Python fallback matches). Selected on
      ~20/24 Bayer and ~14/18 RGB planes; full-frame **Bayer 2.17 → 2.20×, RGB
      2.57 → 2.62×** (+1.6% / +2.0%; the 704-context-no-wrap fix turned an earlier
      Bayer regression into a gain). Decode ~5 s/frame (sequential). 91 tests green.
      Follow-up: a native Paeth reconstruct if Paeth is ever re-enabled.
- [x] **Context-conditional entropy coding** — the CALIC option is now a full
      integrated codec (`calic_codec` in C; byte-identical pure-Python fallback):
      predict + bias + a magnitude-bucket arithmetic model **selected by the local
      gradient energy** (dh+dv quantised to 12 bins) instead of ctxcoder's scan-order
      order-2 context. Since the energy is read from reconstructed neighbours, coding
      is interleaved with the prediction loop (one pass, encode & decode share it).
      Measured +2.6% (Bayer) / +1.25% (RGB) on the residual *coding* vs order-2
      ctxcoder; net full-frame **Bayer 2.20 → 2.22×, RGB 2.62 → 2.64×** (+0.6/+0.7% —
      smaller end-to-end because on big frames ctxcoder's order-2 has plenty of data
      to adapt, so the energy context adds less). 91 tests green. Full image arc from
      plain MED: Bayer 2.12 → 2.22×, RGB 2.57 → 2.64×.
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
- [x] **Real-movie sweep + stronger motion search** — ran the codec on decoded frames
      from a real movie library (`scripts/movie_lossless_benchmark.py`, all local,
      round-trip verified). Splits cleanly by content motion: **animation wins big**
      (claymation +55%, anime +32%, CGI +16%), general live action +3–12%, **high-motion
      loses** (The Gentlemen −18%, Sherlock −6%). Replaced the fixed ±8 integer search
      with a **hierarchical coarse-to-fine** search (÷2 pyramid → ~±19 px range +
      per-block full-res refine; encoder-only, no bitstream change). It moved high-motion
      <1%. Added `videocodec.mode_stats` (shared `_choose_modes` helper, 2 tests) — the
      block-mode mix proves the bottleneck: high-motion is **~89% intra**, only ~5% inter,
      so motion search was never the limiter.
- [ ] **NEXT for video (the high-motion lever): stronger intra.** High-motion frames are
      ~89% intra-coded and our intra is plain MED vs FFV1's context-modelled intra. Upgrade
      the intra path for intra blocks — e.g. the CALIC-class predictor + energy-conditioned
      coding already in `predictors.py` (used by imagecodec). This is the change that would
      convert the high-motion losses; the motion search is not the lever (proven above).

---

## 3. Test more data types

Fits structured / numeric data; useless on already-compressed / encrypted / noise.

- [x] **Auto-detect + dispatch (the `file`-command idea)** — `compressor/detect.py`
      + a `cli identify` subcommand sniffs a file's type (magic bytes for PNG/JPEG/GIF/
      FITS/DICOM/TIFF/CR2/WAV/y4m/npy/gzip/zip/xz/zstd/bzip2/ELF/PDF, then text-content
      heuristics for json/xml/html/code/log/csv/plain) and names the ideal codec.
      `compressor/auto.py` + `cli auto-compress` / `auto-decompress` then *route*: detect →
      build candidate encodings (matching specialist + universal fallbacks) → **verify each
      round-trips byte-exact** → keep the smallest verified, tagged in a 4-byte header so
      decompress routes back. Wired specialists: **.npy** 2D/3D int arrays and **FITS** int16
      images → imagecodec (gray / RGB / inter-slice-delta volume), with the format's
      non-array metadata (npy/FITS headers, padding) preserved verbatim. Because *store*
      always verifies, the result is never larger than the original and never wrong. 8 tests.
      Honest limits: (1) on a tiny image the verbatim format header lets deflate win — auto
      correctly picks the smaller; (2) the text codec is model-based, so auto can't get the
      trained-dict win on arbitrary text without a shipped model and falls back to deflate.
      Open: route the other no-model media codecs (y4m → videocodec, WAV → audiocodec, DICOM).

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

- [x] **Floating-point data** — now a handled type. Added a Gorilla-style XOR-delta
      transform op (`xor`, stride 8/4) + byte-plane-split specs to the repertoire; the
      proxy-selection gate picks it automatically for smooth float data. Measured
      end-to-end in our full codec (`scripts/float_codec_benchmark.py`), held-out
      float64: power Voltage 4.90x vs xz 4.60x, G_active 5.32x vs 5.16x, synth
      random-walk 1.30x vs 1.28x (xor8+split8 selected) — **beats xz/zstd on all three**.
      On the real columns identity wins (full-precision mantissa is noisy); XOR-delta
      helps on genuinely smooth data. Honest: smooth float64 is ~1.3x near the entropy
      floor for anyone, so this is "we win on float now," not "smooth float compresses."
- [x] **FCM/DFCM value predictor** — added the `fcm` transform op (FPC-style: FCM table
      predicts the next value from a hash of recent values, DFCM predicts the next diff;
      XOR with the better predictor, 1-byte selector + byte-plane-split residuals). Exact
      round-trip incl. odd lengths; auto-selected by the gate where value-structure is
      strong — pure linear ramp ~75x over raw (DFCM nails the constant diff), single-freq
      sine wins. On noisier/larger-magnitude data the gate keeps the simpler transforms
      (chunked 4096-value files limit learning; bit-diff prediction weakens across float
      exponents), never regressing. select() ranks the O(n) pure-Python fcm against the
      incumbent on a 256 KB sample so it doesn't tax non-float training (real json/logs/
      html still pick identity). Open: a **native C port of fcm** to remove the
      training-time cost; possibly a per-file predictor-state carry to help small chunks.
- [x] **Seismic** (`scripts/seismic_benchmark.py`) — real broadband waveforms (int
      ADC counts from IRIS; 2010 Chile M8.8 at ANMO + a quiet window, round-trip
      verified). Prediction **crushes** xz: 6.60× / 7.36× vs xz 2.29× / 3.73× — beats
      xz by +97% to +188%, the largest margin of any dataset. Winner: the audio
      codec's fixed-2 + 16/256-tap LMS cascade + `ctxcoder` (generalises directly —
      seismic is a smooth waveform like music). Confirms the prediction niche
      decisively.
- [x] **Columnar / numeric CSV — built (`compressor/csvcolumnar.py`).** The "CSV-aware
      front-end" the earlier probe asked for. Detects a regular delimited grid (delimiter /
      line-ending / constant field count), peels the header row, transposes to column-major,
      and codes each column by type: **fixed-decimal / integer columns → scale to ints →
      delta + ctxcoder** (the real lever — not blanket delta, only where it pays), text
      columns → deflate (homogeneous values grouped). Self-describing container, grid path
      **verified byte-exact at encode**, deflate/store fallback for non-grids → always
      lossless, never larger. On the UCI power CSV (2M rows): **13.4× vs xz 11.3×, zstd 10.1×,
      gzip 7.0×** (+16% over best general). CLI `csv-{encode,decode}`, `scripts/csv_benchmark.py`,
      8 tests (decimals/CRLF/delimiters/ragged/quoted-fallback). Open: quoted-CSV grids that
      keep a constant field count (handled today only when quoting doesn't change the count);
      Parquet/ORC comparison.
- [x] **Scientific / medical images** — tested on **real** public data
      (`scripts/scientific_image_benchmark.py`; pydicom test CT/MR + NASA FITS), both
      **wins** once the codec handles them right (signed int16 + data-driven scale):
        * **DICOM 16-bit medical (CT/MR)**: **4.79× vs PNG-16 3.33×, xz 2.78×** (+44%).
        * **FITS int16 astronomy**: **5.54× vs xz 5.01×, PNG 3.94×** — a win. (An earlier
          1.86× "loss" was a measurement bug: viewing signed int16 as uint16 wrapped
          negatives into huge jumps that wrecked prediction; with correct signed +
          endian handling it beats everything.)
        * **FITS float32**: ~1.2× for everyone (near the entropy floor), like float64.
- [x] **LZ pre-pass + data-driven scale + 3D inter-slice delta** (imagecodec v4):
        * **RLE coder** (selector 4) added to the per-plane choice — the LZ-style pass
          for sparse / mask / label planes (large constant regions): auto-wins where a
          predictor can't (127× on a 99.5%-zero image, beats CALIC on binary masks),
          while CALIC keeps dense planes. No regression (selection picks the smallest).
        * **Data-driven scale** — the GAP/CALIC gradient threshold scale is now chosen
          per plane from its value range (candidates tried, best stored), so low-range
          16-bit (+9% on FITS) and the small inter-slice deltas get tracked thresholds.
        * **3D volumes** — `encode_volume`/`decode_volume`: slice 0 direct, later slices
          as inter-slice deltas. **+31%** over per-slice on a correlated volume. 94 tests.
      Open: HDF5. (CSV transpose front-end now built — `compressor/csvcolumnar.py`.)
- [x] **Terrain DEM + hyperspectral** — two new scientific niches, public data, round-trip
      verified (`scripts/dem_benchmark.py`, `scripts/hyperspectral_benchmark.py`):
        * **DEM (SRTM int16 elevation)** — smooth height fields are squarely the predictor's
          domain: **4.49× vs PNG-16 2.81×, xz 2.64×, zstd 2.21×** (1.60× over the best),
          a clean win straight through the gray image codec.
        * **Hyperspectral (AVIRIS Indian Pines, 200 bands)** — closes the open
          "de-interleave bands + delta" item: feeding bands as volume slices, **inter-band
          delta gives +14% over per-band** (2.41× vs 2.08×) and beats xz 1.83× / zstd 1.65×.
- [x] **Genome DNA (FASTA) — honest boundary** (`scripts/genome_benchmark.py`). DNA is a
      near-uniform 4-symbol source (~1.95 bits/base at order 2–4): **2-bit packing (4.05×)
      is the floor and prediction/transforms add nothing**; xz gets 3.72×, our codec has no
      edge. Like json vs `zstd --train`, this is where specialists (high-order DNA context
      models) win — documented, not chased.
- [x] **LiDAR point cloud + protein** — a new structural domain and the alphabet-boundary midpoint:
        * **LiDAR (LAS, `scripts/lidar_benchmark.py`)** — de-interleave the interleaved point
          records into typed columns and first-difference the spatial fields (X/Y/Z/intensity/
          GPS/RGB): **4.20× vs xz 2.88×, zstd 2.54×** on airborne LiDAR (110K pts), round-trip
          per column. Beats general codecs; LAZ (LASzip) is the ~5–15× specialist (not run —
          no laszip). A genuinely new structure (irregular 3D geometry) and the clearest case
          yet for a columnar/transpose front-end (cf. the open CSV-transpose item).
- [x] **Columnar front-end built (`compressor/columnar.py`).** A real codec module for
      fixed-width binary record streams: a *schema* (list of field byte-widths in {1,2,4})
      de-interleaves records into per-field integer columns, each coded as the smaller of
      raw / first-difference under `ctxcoder`; self-describing container; *store* fallback so
      it never expands. Caller passes an exact schema (LAS from its header) or a width to
      search uniform tilings, or neither to auto-detect the record period (byte
      autocorrelation). CLI `columnar-{encode,decode}`; 6 round-trip tests. The LiDAR
      benchmark now runs through it. Honest: **`sao` stays a boundary** — the codec
      correctly detects its 28-byte records and aligns at offset 0, but the star catalog's
      float fields aren't sorted so columns don't delta-compress (1.26× vs xz 1.64×); not all
      record data is columnar-friendly.
- [x] **Second-difference (Δ²) per-column coding** — both columnar codecs now try raw /
      delta / **double-delta** and keep the smallest (so it never regresses). Δ² wins on
      monotonic or linear-trend columns (GPS time, sequential ids, timestamps): **LiDAR
      4.20× → 4.77× (+14%)** as its coordinate + GPS columns prefer Δ². CSV unchanged (its
      columns aren't ramps). A measured, safe ratio gain across the columnar family.
- [x] **Wired into `auto`** — `auto_compress` now routes **text → csvcolumnar** (CSV/TSV
      transpose, deflate fallback) and **opaque binary → columnar** (auto record-period
      detection), verify-byte-exact + keep-smallest like the other routes. Measured: power CSV
      → csv 11.9×, LiDAR point region → binary-columnar 4.0× (schema-free). New `.az` methods
      M_CSV / M_COL. Open: leading-offset detection so whole headered files (e.g. a full .las)
      auto-route too.
- [x] **Climate / weather float grids (HDF5)** — `scripts/weather_benchmark.py` (also closes
      the open HDF5-reading item via h5py). NCEP reanalysis float32 (366×73×144): **boundary**
      — smooth float32 compresses for everyone (xz 3.20×), our float decorrelators don't beat
      it (1.65×), and values don't map losslessly to scaled ints (float32 rounding). Like FITS
      float32. Lossless float beyond xz needs a dedicated float predictor (open).
        * **Protein (FASTA AA, `scripts/protein_benchmark.py`)** — *boundary*, completing the
          alphabet story: a ~20-symbol near-i.i.d. source (~4.15 bits/residue, no order-1/2
          gain). Order-0 entropy coding *beats* the LZ tools (xz 4.60 bpr) since there's no
          repetition, but prediction/transforms add nothing structural — same lesson as DNA
          at 4 symbols. So small symbolic alphabets (4 → 20) are entropy-bound boundaries.
- [x] **Recognized public corpora** — ran the named compression benchmarks, all round-trip
      verified, methodology matched to our amortized/specialist design:
        * **enwik8** (LTCB Wikipedia, `scripts/enwik_benchmark.py`): held-out **3.06× beats
          gzip/zstd/xz/bzip2**, ~6% behind `zstd --train`.
        * **Kodak** (24-image lossless set, `scripts/kodak_benchmark.py`): **beats PNG on
          24/24 (+27%)**, within a few % of the modern best (JPEG-XL −6%, WebP-LL −2%).
        * **Silesia** (routed per-type, `scripts/silesia_benchmark.py`): `mr` MR-volume +21%
          and `x-ray` +18% vs xz; held-out text (1 MB train) beats every standard tool on
          dickens/webster/reymont/`samba`/`nci` (trails only `zstd --train`); loses on `xml`
          (repetitive markup — LZ/BWT/zstd win, confirmed across test regions, *not*
          training-limited) and `sao` (float records — int16 view is wrong, needs column
          routing); binaries (mozilla/ooffice/osdb) are not our design. Calgary/Canterbury
          skipped: single arbitrary files where our amortized model overhead misrepresents
          the design (self-contained single-file expands; the dictionary *is* the model).
- [x] **Memory-bounded training (fixed an OOM that capped corpus size).** The parallel
      blob-spec search (`model._search_costs`) fanned across *all* CPUs, each worker holding
      a substring Counter (~1.5 KB per byte of fit, ≈1.5 GB at 1 MB), so training >512 KB
      OOM'd (2 MB → >10 GB). Now `_worker_cap` sizes the pool by free RAM (`MemAvailable`,
      ~1.5 KB/byte/worker, 60% margin) → serial when tight. This *unblocked* 1 MB training,
      which is the dictionary miner's saturation point (`max_mining_bytes`): on **source code
      it flips the result** — `samba` held-out 1.86→**1.67 bits/char**, from beating only gzip
      at 512 KB to **beating gzip/bzip2/xz/zstd** at 1 MB. `xml` does *not* benefit (it's
      LZ/BWT-favourable markup, an honest boundary). Most text improves with the fuller corpus.
      Side benefit: the trained-type benchmarks (`cli benchmark json/html/...`, corpora 1.5–4 MB)
      could OOM under the old all-CPU fan-out (8 workers × 1.5 GB > RAM); they now run safely.
- [x] **Headroom check — the shipped text types are already saturated (measured, no change).**
      The samba flip was a *one-off*: the Silesia held-out under-fed it at 512 KB. The real
      per-type corpora (json/logs/html/xml/code) already train at ≥1 MB, so they sit at the
      mining cap. Swept both levers on held-out: **more mining data (2–3 MB) is flat** (json
      9.39→9.40×, html 7.55×, logs/xml unchanged), and **more patterns hurts** (json 9.39→9.15×,
      html 7.55→7.45× at 16 384 — the larger token alphabet costs more than the extra patterns
      save; only `code` gains a marginal +0.6% at 8 192). So **4096 patterns / 1 MB mining is the
      tuned sweet spot** for these types — no headroom to harvest; don't re-chase it.
- [~] **More text formats** — added **source code** (Python) as a trained type
      (`scripts/collect_corpus.py`): held-out, **ours 5.82× beats plain gzip/zstd +55%**
      but trails `zstd --train` 6.26× by ~7% — like json, it's cross-file-repetitive
      text where zstd's COVER+FSE win. Also added **XML** — held-out **ours 8.29×
      beats `zstd --train` 7.80× (+6%)** and plain gzip/zstd by +140% (verbose,
      tag-repetitive markup is the blob's sweet spot, like html). Both reproducible via
      `cli benchmark {code,xml}`. Open: YAML, TOML, CSV (the local CSVs are text, not
      numeric, so a columnar transform wouldn't help). FASTA done (boundary — see below).

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
- [x] **beat `zstd --train` on text (logs & html)** — scaled the trained blob to the
      512 KB LZ match window (`BLOB_SPECS` up to 1<<19; validation gate picks per
      type). Real corpus, round-trip verified, vs zstd at its **best** dict size
      (benchmark now trains 110/256/512 KB and reports zstd's cheapest): logs 15.12×
      vs 14.06×, html 7.55× vs 7.08× — beating its best, not a fixed default.
- [~] **Beat `zstd --train` on json** — narrowed but not closed. Two real wins
      landed (both help every small-file type): a varint container header
      (26 → ~12 B/file) and a depth-16 repeat-offset cache (json has ~30% recurring
      match distances; depth-3 caught ~10%, depth-16 ~27%). json 54.5 → **52.7 KB**,
      gap to zstd 4.8 → **3.0 KB (−38%)**. Still 6% behind (zstd 49.7 KB @256 KB
      dict). The gap is **not** the dictionary (zstd's own 256 KB dict in our codec
      is no better) and **not** the literals (per-token breakdown: order-0 arithmetic
      is already near-optimal; order-1 context *regresses* on the residual unique
      strings/numbers) and **not** offset entropy coding — the distance extra bits
      are provably ~incompressible (a per-slot context model recovers only ~178 B of
      11.2 KB; they are uniform within each octave, so "FSE offsets" buys nothing).
      The gap is purely zstd's **repeat-offset-aware optimal parser**: json is
      fragmented (~9.7 K matches, avg 44 B), and zstd restructures the token sequence
      to turn more of those into near-free rep-hits; ours prices every match as a full
      distance and can't. BUT a ceiling test (one-off, see git history) showed even
      that lever is small: only 2.5% of matches have an equal-length match
      available at a cached distance (json's matches hit too many distinct blob
      positions), so rep-aware distance-swapping saves just ~186 B. **Conclusion: no
      single lever closes the ~2 KB gap** — not the dictionary, literals, offset
      entropy, rep cache, deeper search, or a rep-aware parser. It is the diffuse sum
      of zstd's mature, integrated parser+coder. Marking this **won't-fix** unless we
      commit to reimplementing zstd's sequence coder wholesale (high effort, no
      demonstrated win). (Measured aside: the parse is search-limited — bumping
      `max_chain` 128 → 2048 alone recovers ~1 KB, json → ~51.7 KB / ~4% behind, at a
      real speed cost; a candidate default bump independent of the parser question.)
- [ ] More **transforms**: 2D predictors, RLE for the zero-runs decorrelation
      produces, channel de-interleaving.
- [x] **Audio: third LMS stage** — added a 512-tap (shift 14) stage after the
      16/256 cascade. Measured on real music: mean ratio 1.90x → 1.92x, FLAC
      advantage +5.9% → +7.4% (better on 11/12 tracks). 512,14 beat a 1024,15
      variant and is cheaper.
- [ ] **Audio**: still longer / per-track-adaptive filter orders; definitive
      comparison vs `flac -8` (needs the `flac` binary, on the unmounted NAS).
- [~] **Faster training.** The blob-spec validation search (9 independent specs,
      ~80% of training time) is now **fanned out across processes** (`_search_costs`,
      ProcessPoolExecutor; order-preserved so the cheapest-wins pick is identical to
      serial; gated to corpora ≥512 KB so small models/tests stay serial). json
      training 232 s → 127 s on 8 cores (~1.8×; sub-linear due to memory-bandwidth
      contention from concurrent 512 KB-blob tokenisation + the serial final rebuild).
      Still open: reuse blob hash chains across specs/files; rep-offset-aware parse.
- [x] **Faster image encode.** GAP (selector code 2) was measured to never win once
      CALIC is in the trial set (0/56 planes — CALIC subsumes GAP's prediction), so
      it's dropped from the encoder's per-plane trial (now MED + CALIC, 2 not 3).
      Zero compression change (GAP never selected; decode still honours the selector);
      encode ~35–44% faster (Bayer 7.2 → 4.7 s, RGB 13.1 → 7.4 s per 21-MP frame).
