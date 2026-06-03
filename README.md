# Per-File-Type Trained Lossless Compressor

A lossless compressor built on one idea: **learn the common patterns of a file
type, then encode files as short references to those patterns plus short codes
for frequent bytes.**

Two intuitions, realized honestly:

- *"256 patterns make up a file"* → a **trained dictionary** of common multi-byte
  chunks, with a literal-byte fallback so any file rebuilds byte-for-byte.
- *"compress 8 bits to 4 bits"* → **arithmetic entropy coding**: frequent
  patterns/bytes cost a fraction of a bit, rare ones more, so the average drops
  well below 8 bits/byte — without losing anything. (A Huffman coder is also in
  the tree as a tested building block, but the pipeline uses arithmetic coding,
  which spends fractional bits and tracks the true entropy more closely.)

On top of the cross-file dictionary, the codec also uses **LZ77 back-references**.
When LZ is enabled, training prepends a learned **blob** to each file's history,
so matches can reach into arbitrary substrings of trained content — the way zstd
uses a dictionary — as well as in-file repetition. Two blob builders are
available: **naive** (whole training files concatenated, preserving long
contiguous runs) and **coverage** (zstd-COVER-style: pack the most
frequently-referenced content, deduplicated, most-useful nearest the data).
Training tries dict-only plus both builders at several sizes and keeps whichever
is cheapest **on a held-out validation slice** (so the blob can't overfit the
choice). Different types land on different strategies — see results below.

The twist that beats general-purpose tools: the model is **trained per file type
and shipped separately**, not embedded in every compressed file the way gzip is.
That cost is paid once and amortized across many files. The honest win-scenario
is therefore **many smallish files of a known type** (API responses, log lines,
HTML pages).

Beyond text, the same **predict-then-entropy-code** idea — a per-type reversible
**transform**, a context-adaptive residual coder, a dedicated adaptive-filter
audio codec, and a motion-compensated video codec — extends across domains.

**Results at a glance** (every number on real data, every result round-trip verified):

| domain | data | our result vs the standard codec |
|--|--|--|
| **text** | JSON / logs / HTML (held-out) | beats plain gzip/zstd 29–62%; **beats `zstd --train`** on logs & html, ~4% behind on json |
| **raw image** | Canon CR2 Bayer | **statistical parity with JPEG XL**; beats Canon / PNG / zstd |
| **audio** | 16-bit PCM music | **beats FLAC +7.4%** (9/10), and **beats xz +59%** (1.96× vs 1.24×) |
| **biosignal** | ECG (PhysioNet) | **beats xz +7%** (3.06× vs 2.94×) |
| **seismic** | broadband waveforms (IRIS) | **beats xz 2–3×** (6.6–7.4× vs 2.3–3.7×) |
| **sensor numeric** | UCI power (int columns) | 6.27× — beats gzip; xz wins (repetition-heavy) |
| **video** | CIF clips (full YUV) | **beats FFV1 +8% to +53%** (motion compensation) |

The unifying result, and the dividing line: **predict per type, then entropy-code.**
Where a signal is smooth or structured (audio, ECG, raw images, video, slowly
varying sensors), an adaptive predictor + context-adaptive arithmetic beats the
general-purpose tools and even the domain specialists. Where data is *repetition*-
dominated (long constant runs, exact repeats), LZ-family coders (`xz`,
`zstd --train`) win, and we don't pretend otherwise. Floating-point is a boundary
the integer transforms don't cross (general coder on raw bytes is best there).

Everything runs through a **native C hot path** (via ctypes), bit-identical to the
pure-Python reference with a fallback: `compress` of a 0.8 MB text file went
111 s → 0.78 s (~140×), so the whole family — text, audio, video, numeric — is
fast enough to use, not just a ratio demo.

## How it works

```
train(corpus)                         compress(file, model)
  select transform      ─┐              apply transform (decorrelate)
  mine patterns + blob   ├─ model        └─ cost-optimal parse (DP over tokens)
  price modes on val set ┘                   └─ arithmetic-code the token stream
  pick cheapest                                  └─ container = header + bitstream
```

A reversible **transform** runs first (and is inverted last), chosen per file
type by the validation gate: generic byte-stream ops — *delta* (predict from the
byte N back) and *split* (deinterleave into N byte-planes) — that decorrelate
numeric/image data so the coder has far less to encode. Text selects identity.

For LZ types the parser is **cost-optimal**: a dynamic program finds the
minimum-cost path through the token graph, pricing every candidate (literal,
dict ref, each LZ match) by its actual arithmetic-coded bit cost. Dict-only
types keep the cheap greedy longest-match parse.

Tokens are literals, dictionary references, or `(length, distance)` LZ matches.
Match lengths and distances are bucketed into slots (one coded symbol + a few
raw "extra" bits each), with a separate frequency model for distances. LZ
matches use **lazy parsing** (one-byte lookahead: defer a match if the next
position offers a longer one); dictionary matches commit greedily since they're
the cheapest token. Recently-used match distances are cached as **repeat
offsets**, so a match reusing one codes a tiny index instead of a full distance.

Decompression reverses it and verifies a CRC32, so losslessness is checked on
every file.

## Modules

| file | responsibility |
|------|----------------|
| `compressor/bitio.py` | MSB-first bit reader/writer |
| `compressor/arithmetic.py` | integer arithmetic coder (Witten–Neal–Cleary) |
| `compressor/freqmodel.py` | static frequency model driving the coder |
| `compressor/huffman.py` | canonical Huffman (package-merge) — tested building block |
| `compressor/transform.py` | reversible per-type decorrelating transforms (delta/split) |
| `compressor/dictionary.py` | pattern miner + longest-match lookup |
| `compressor/tokenizer.py` | reversible file ↔ token stream (dict + LZ) |
| `compressor/model.py` | train / save / load a per-type model |
| `compressor/codec.py` | compress / decompress + container + checksum |
| `compressor/audiocodec.py` | standalone lossless audio codec that beats FLAC (numpy) |
| `compressor/ctxcoder.py` | context-adaptive arithmetic residual coder (beats xz on ECG) |
| `compressor/videocodec.py` | lossless video codec: motion-compensated inter-frame (numpy) |
| `compressor/native.py` + `_native/audio.c` | C hot loops (ctypes), auto-built, with Python fallback |
| `compressor/benchmark.py` | comparison vs gzip / zstd / zstd-trained-dict |
| `compressor/cli.py` | `train` / `compress` / `decompress` / `benchmark` / `video-encode` / `video-decode` |

## Usage

```bash
# Generate sample corpora (disjoint train/test) for json, logs, html
python3 scripts/make_corpus.py                 # synthetic, reproducible
python3 scripts/collect_corpus.py              # real files from this machine -> corpus_real/

# Train a model for one type
python3 -m compressor.cli train json corpus/json/train -o json.model

# Compress / decompress a single file
python3 -m compressor.cli compress some.json -m json.model -o some.json.cz
python3 -m compressor.cli decompress some.json.cz -m json.model -o roundtrip.json

# Benchmark against gzip and zstd on the held-out test set
python3 -m compressor.cli benchmark json                      # synthetic corpus
python3 -m compressor.cli benchmark json --root corpus_real   # real-world corpus

# Lossless video: encode/decode a .y4m (4:2:0/4:2:2/4:4:4/mono, byte-exact)
python3 -m compressor.cli video-encode clip.y4m -o clip.vid
python3 -m compressor.cli video-decode clip.vid -o roundtrip.y4m
```

Cross-domain benchmark scripts (each compares ours vs the domain's standard codec):

| script | domain | competitors | needs |
|--------|--------|-------------|-------|
| `scripts/image_benchmark.py` | icons / graphics | gzip, zstd, PNG | Pillow |
| `scripts/cr2_benchmark.py` | Canon raw crops | gzip, zstd, PNG-16 | rawpy, numpy |
| `scripts/full_raw_benchmark.py` | full raw frame | gzip, zstd, PNG-16 | rawpy, numpy |
| `scripts/cr2_multiframe.py` | raw, many frames | **JPEG XL** | rawpy, numpy, imagecodecs |
| `scripts/audio_benchmark.py` | audio (generic codec) | **FLAC** | soundfile, numpy |
| `scripts/audio_codec_benchmark.py` | audio (dedicated codec) | **FLAC** | soundfile, numpy |
| `scripts/ecg_ctx_coder.py` | biosignal (ECG) | **xz** | numpy |
| `scripts/scidata_ctx_benchmark.py` | sensor numeric (int) | gzip, xz | numpy |
| `scripts/float_benchmark.py` | floating-point | gzip, zstd, xz | numpy |
| `scripts/video_ffv1_benchmark.py` | video (full YUV) | **FFV1**, JPEG XL | imagecodecs, imageio-ffmpeg, numpy |

## Dependencies

- **Core text/byte compressor and tests: zero external dependencies** (Python 3
  stdlib only — `codec.py`, `model.py`, `tokenizer.py`, `ctxcoder.py`, etc.).
- **`audiocodec.py` / `videocodec.py`** (the media codecs) and the `ctxcoder`
  native path: need `numpy`. The native hot path also needs `gcc` (built on
  import; falls back to pure Python if absent — see below).
- **CLI**: `video-encode` / `video-decode` need `numpy`; `benchmark` uses the
  `gzip` and `zstd` command-line tools. The text `train` / `compress` /
  `decompress` commands stay zero-dependency.
- **Cross-domain benchmark scripts** need the libraries in the table above —
  install with: `pip install pillow rawpy numpy imagecodecs soundfile imageio-ffmpeg`
  (`imagecodecs` bundles libjxl for JPEG XL; `soundfile` bundles libsndfile for
  FLAC; `imageio-ffmpeg` bundles a static ffmpeg for the FFV1 video baseline).
  These are *only* for the optional benchmarks, never the codec itself.

## Native acceleration (the optimised port)

Pure Python validated the *ratios*; for speed, the hot loops are ported to C
(`compressor/_native/audio.c`), compiled to a shared library by `gcc` on first
import and called via `ctypes` (no Python.h needed) — see `compressor/native.py`.
Each native function is **bit-identical and byte-interchangeable** with its
pure-Python reference (verified in tests), so output is unchanged and a file
compressed on one path decompresses on the other. If `gcc`/`numpy` is absent,
everything falls back to pure Python (`native.HAVE_NATIVE == False`), and the
text/byte core stays zero-dependency (native is imported lazily).

Ported so far, with measured speedups:

| primitive | speedup | effect |
|-----------|---------|--------|
| audio LMS filter (256-tap) | ~25× | the audio codec's dominant cost |
| audio fixed-2 predictor + adaptive Rice | — | removes the remaining Python loops |
| byte-stream `delta` transform | ~133× | raw/numeric path (42 MB frame delta: seconds → ms) |
| context-adaptive arithmetic coder (`ctxcoder`) | ~45–60× | the coder that beats xz on ECG: a record went 12.6 s → 0.28 s to encode |
| text/LZ codec arithmetic loop (`codec.py`) | enc ~27× / dec ~46× | the per-symbol token coder (3 freq models + repeat-offset cache + slot bits), byte-identical |
| LZ match-finder (`lz_forward`) | ~15× (whole optimal parse) | the 3-byte hash-chain search + `_match_len`, 61% of the parse; integer-exact candidates → identical tokens. `compress` of 0.8 MB text: 111 s → 7.6 s |
| video MED reconstruction (`med_fill`) | ~2.6× decode (motion clips; more on intra-heavy) | the causal per-pixel intra-reconstruction loop in `videocodec.decode`, byte-identical |
| greedy match-finder + dict matcher (`lz_best`, `dict_match_all`) | compress 7.6 s → 2.9 s; train 103 s → 67 s | the per-position search for the greedy/lazy parse (training) and the trained-dictionary longest-match; integer-exact → identical tokens |
| cost-optimal backward DP (`lz_dp`) | compress 2.9 s → 0.78 s | the parse's DP, on a match-cost lookup table; double arithmetic bit-identical → identical tokens. **End-to-end `compress` of 0.8 MB: 111 s → 0.78 s (~140×).** |

The arithmetic coder is pure integer math, so the C port reproduces the
Witten–Neal–Cleary state machine and MSB-first bit I/O exactly — its output is
byte-identical to the Python coder (verified both directions on random and real
data). The same WNC machine now also drives the **text/LZ codec** (`codec.py`):
its whole per-symbol token loop — three frequency models, the repeat-offset
cache, and the length/distance slot bits — is in C, so the entropy stage encodes
~27× / decodes ~46× faster, byte-identical. Net: the FLAC-beating audio codec now
does **~12 s of audio in ~0.4 s each way** (was minutes), and the context coder
is fast enough to use in anger. The **entire LZ parse** is now native too — the
match-finder (`lz_forward`/`lz_best`), the trained-dictionary matcher
(`dict_match_all`), and the cost-optimal backward DP (`lz_dp`) — every stage
integer- or bit-identical to the Python reference, so the produced tokens are the
same. End-to-end **`compress` of a 0.8 MB text file went from 111 s to 0.78 s
(~140×)**, and the whole compress/decompress hot path now runs in C. The only
remaining pure-Python cost is *training*-side (pattern mining + blob building),
not compression.

## Tests

```bash
python3 -m tests.run            # all tests (no dependencies)
python3 -m tests.run codec      # one module
```

The codec tests include property-style round-trips over random bytes, empty
input, bytes never seen in training, and a numeric/transform round-trip — proving
the lossless guarantee.

## Results

Ratio = raw ÷ compressed (higher is better). Two corpora: **synthetic**
(`scripts/make_corpus.py`, reproducible) and **real-world** files collected from
this machine (`scripts/collect_corpus.py`). The two tell different stories — read
both.

### Real-world corpora (real files, held-out) — the honest test

| type | gzip -9 | zstd -19 | zstd -19 +dict | **ours** |
|------|---------|----------|----------------|----------|
| json | 5.70x | 6.18x | 9.42x | 9.08x |
| logs | 7.40x | 7.76x | 14.06x | **14.34x** |
| html | 3.86x | 3.98x | 7.08x | **7.49x** |

On real, heterogeneous files we **beat plain gzip / zstd -19 by 29–62%**, and —
after scaling the trained **blob** to the 512 KB LZ match window — we now **beat
`zstd -19 --train` on logs and html**, and reach **96%** of it on json. The blob is
prepended to each file's history and shipped once (amortised, like zstd's
dictionary), so a larger one just means more cross-file content to match; the
validation gate picks the size per type. The comparison is fair: on logs/html
zstd's *larger* dictionaries were actually *worse* (110 KB is its best there), so
we beat its best. On **json**, zstd stays ahead (49.7 KB with a 256 KB dict vs our
54.5 KB), and a controlled experiment pins down *why* — and it is **not** the
dictionary. Feeding zstd's *own* 256 KB COVER dictionary into our codec as the blob
gives 54.1 KB — barely better than our own blob, and still **8.8% behind zstd using
the identical dictionary**. So the json gap is our codec's **coding efficiency**:
decomposing it, ~82% is entropy/offset coding (our distance "extra" bits are coded
*raw/uniform*, while zstd FSE-codes offsets with a repeat-offset history) and ~18%
is excess container header (26 B/file vs zstd's ~14 B). 86% of json bytes are LZ
matches into the blob, so it is dominated by match-offset cost. Closing it would
need FSE-level offset/literal entropy coding — a major rewrite, not worth it given
we beat zstd everywhere else. The shipped model is large (real html ~1.5 MB), so it
only amortizes over many files.

### Synthetic corpora — where we win (but it's partly overfit)

| type | gzip -9 | zstd -19 | zstd -19 +dict | **ours** |
|------|---------|----------|----------------|----------|
| json | 1.98x | 2.02x | 5.46x | **6.50x** ✅ |
| logs | 3.80x | 3.99x | 5.95x | **6.27x** ✅ |
| html | 2.72x | 2.70x | 10.70x | **11.41x** ✅ |

On the synthetic corpus we beat `zstd +dict` on all three types — but the
synthetic files are highly homogeneous, which flatters our approach. The
real-world numbers above are the truer measure; the gap between the two tables is
itself the lesson: **validate on real data.**

Takeaways:

- **We beat standard `zstd -19` everywhere** (real data: +29–62%), and on the
  synthetic corpus we beat even `zstd --train`. The pipeline compounds: trained
  dictionary, contiguous LZ blob, cost-optimal parse, repeat offsets, arithmetic
  coding.
- **We do not beat `zstd --train` on real, heterogeneous data** — we reach
  77–91% of it. Our synthetic wins were partly overfit; real files corrected the
  picture. zstd's remaining edge is a more byte-efficient (COVER-trained)
  dictionary plus FSE coding.
- The **blob builder and size are chosen per type on a validation slice** (naive
  vs COVER-style coverage, 32–128 KB), so a strategy only helps where it helps and
  never regresses a type.

Honest costs:

- **Model size** grows with the blob and dictionary (real html ~1.1 MB). It ships
  once and amortizes across many files, but on heterogeneous data that amortizes
  less well — and it is much larger than zstd's 110 KB dictionary.
- **Training is slow** and **cost-optimal parsing doesn't scale to large files**
  in pure Python (real html — ~16 KB/file — took many minutes). Compression and
  decompression of small files are fine; large-file throughput needs work.

## Image domain — a cross-domain stress test

Images map out exactly where the approach has value. Each image is decoded to
raw pixel bytes and every method compresses identical data; **PNG** is the
lossless-image baseline. Tools: `scripts/image_benchmark.py` (PIL),
`scripts/cr2_benchmark.py` and `scripts/full_raw_benchmark.py` (rawpy/LibRaw).

| data | gzip | zstd -19 | zstd +dict | PNG | **ours** | rank |
|------|------|----------|------------|-----|----------|------|
| tiny icons (16–96 px, homogeneous) | 3.43x | 3.60x | 4.82x | 2.37x | **5.39x** | **1st** |
| flat UI graphics (256 px) | 25.90x | 30.90x | 30.54x | 25.70x | **30.70x** | tied top |
| Canon CR2 raw Bayer (photographic) | 1.46x | 1.56x | 1.52x | 1.39x (PNG-16) | **1.84x** | **1st** |

(The raw row is crop-level, ranked among the columns shown; the full-frame
comparison against **JPEG XL** — the real state-of-the-art — is in the bullet
below. Canon's own full-frame lossless ≈ 1.6–1.75x. Raw sensor noise is
near-incompressible: these ratios are close to the information-theoretic floor.)

The result is consistent with the text findings: **we win where redundancy
exists** — and the transform stage now exposes redundancy we previously couldn't.

- **Icons — we beat everything, including `zstd --train` and PNG.** Tiny files
  drown PNG in per-file overhead, and PNG compresses each image independently, so
  it cannot use the shared palette/style across an icon theme; our cross-image
  trained dictionary can. A genuine niche (sprite atlases, icon themes, map tiles).
- **Flat graphics — we tie zstd and beat PNG**, thanks to large LZ-able regions.
- **Photographic raw — from dead-last to parity with JPEG XL.** Raw was our worst
  case (1.51x, last) until the **transform stage**: we measured the entropy (10.27
  bits/pixel order-0, 6.87 after prediction) and added a reversible per-type
  transform (here `delta(4)` then byte-plane `split(2)`) that decorrelates the
  16-bit mosaic before coding. zstd/gzip/PNG can't infer that structure from
  opaque bytes; our per-type gate discovers it from the data.

  A full 8-frame sweep of real Canon raw vs **JPEG XL lossless** (`cjxl -d 0`, the
  state-of-the-art) — `scripts/cr2_multiframe.py`:

  | | Canon | JPEG XL | **ours** | ours+model |
  |--|-------|---------|----------|------------|
  | mean over 8 frames | 1.60x | 1.89x | **1.90x** | 1.86x |

  We **match JPEG XL** (1.90x vs 1.89x mean), trading the lead frame-to-frame —
  ours wins the more-compressible frames, JXL the noisier ones (its learned
  predictor extracts more from near-pure noise). Counting our shipped ~0.5 MB
  model, JXL is marginally ahead (1.89x vs 1.86x; it wins 5/8 frames). Both
  decisively beat Canon's own codec. Caveat: JXL is 1-pass and ~40 s; ours is
  2-pass, self-trained per frame, and minutes in pure Python — JXL is far more
  practical. The result is **statistical parity, not a win** — but reaching it
  with a from-scratch byte coder + one auto-discovered transform, no hardcoded
  image knowledge, is the point.

## Audio domain — building a codec that beats FLAC

Lossless audio (16-bit PCM, real music) decoded via libsndfile; **FLAC** is the
purpose-built baseline.

**First, the generic codec + transform falls short.** The per-type transform
auto-selects `delta(4)+split(2)` and beats gzip/zstd (which are near-helpless on
PCM), but FLAC wins decisively — 1.16x vs 1.59x. The reason: a stride-delta is
only a *1st-order* predictor, and audio rewards *adaptive high-order* prediction.
A simple transform can't reach FLAC.

**So we built a dedicated audio codec** (`compressor/audiocodec.py`,
`scripts/audio_codec_benchmark.py`) — Monkey's-Audio-style, all integer and
exactly reversible: mid/side → fixed order-2 predictor → cascade of integer
sign-sign LMS adaptive filters (16 + 256 + 512 tap) → adaptive Rice. The filters learn
online from the reconstructed signal (nothing shipped), and adaptive Rice tracks
the per-sample magnitude, beating FLAC's per-partition Rice. Over 10 real tracks
(bit-exact verified each):

| | gzip -9 | zstd -19 | FLAC | **ours** |
|--|---------|----------|------|----------|
| mean | 1.10x | 1.12x | 1.80x | **1.92x** |

**Ours beats FLAC on 9/10 tracks, mean +7.4%** (up to +22%). And against **xz**
directly on the PCM (where music gives LZ nothing to grab): ours 1.96× vs xz 1.24×
— **+59% on 8/8 tracks**. This is the flip side of the power result: high-entropy
smooth signals are exactly where prediction crushes a general LZ coder. The third
(512-tap) LMS stage added +1.5 points of margin over the prior two-stage cascade
(measured on 12 tracks, better on 11/12). Caveats: vs
libsndfile's FLAC (the `flac -8` CLI may be ~1–3% stronger); measured on 3 s
chunks where our adaptive filters only partly converge (full tracks likely favour
us more); and pure-Python, so slow — a *ratio* result, not a fast codec.

A **second entropy back-end** is now selectable (`encode(..., coder="ctx")`):
context-adaptive arithmetic coding (`compressor/ctxcoder.py`). It does *not* help
here (the LMS cascade already whitens the residual, so Rice's per-sample
adaptation wins — 1.84x vs ctx 1.82x over 12 tracks), but it wins decisively on
*weakly*-predicted signals — see the next section.

This is the sharpest version of the unifying lesson. A **cheap generic transform**
closes the gap to a specialist only by as much as the specialist exceeds simple
decorrelation — enough for Bayer (→ JPEG-XL parity), not for audio. But a
**domain-specific adaptive predictor**, when the structure demands it, can beat
the specialist outright. The architecture tells you which you need: try the cheap
transform first; reach for a real predictor only where it doesn't suffice.

## Scientific numeric time-series — a reality check

Tested on two real public datasets in exact lossless representations, every
result round-trip verified, against gzip/zstd/xz (`scripts/scidata_*`,
`scripts/ecg_*`). The headline: the same **`delta + ctxcoder`** is the right tool
across both types tested — it beats `xz` on ECG and closes most of the gap on
repetitive sensor data (and the one apparent "loss" turned out to be a wrong
coder choice, not a real limitation).

**Repetitive sensor data: the *coder* mattered, not LZ.** UCI household power
(2.05 M rows × 7 sensor columns, exact int32 milli-units): **51 % of deltas are
exactly zero** — long constant runs (appliances off, coarse quantisation). The
first pass used the memoryless adaptive **Rice** coder (delta+Rice = 2.78×) and
concluded "this needs LZ, which our fast path lacks". *That was wrong about the
remedy.* Running the order-2 **`ctxcoder`** (built for ECG, but never tried here)
on the same delta gives **6.27×** (beats gzip's 6.15×) — the order-2 context makes
a run of zeros cheap (after a zero, the conditioned bucket→0 probability is high),
so the 95 %-zero column `Sub_1` goes from ~2.5× (Rice) to **41.7×** (ctx). It's
still short of xz's run-length LZ on those columns (`Sub_1` 111×; see "Can we beat
xz" above), but the headline correction stands: the original "needs LZ" verdict was
wrong as a *remedy* — the right coder more than doubled the ratio. See
`scripts/scidata_ctx_benchmark.py`.

| household power | gzip | xz -9 | delta+Rice (old) | **delta+ctx** |
|--|--|--|--|--|
| ratio | 6.15x | **8.56x** | 2.78x | **6.27x** |

So we now beat gzip and close most of the gap to xz (was 3× behind); xz's
stronger LZ + range-coder context still edges us on the very runniest data. (Our
*own* LZ codec is not the answer here: on the full file it gets only 5.82× — worse
than delta+ctx and gzip — and its cost-optimal parse is pathologically slow on
run-heavy data, ~30 min vs ~4 s, since the all-zeros 3-byte key builds enormous
hash chains. The order-2 context coder captures the runs better *and* faster.) The
lesson: the same `delta + ctxcoder` is the right tool for *both* repetitive sensor
data and smooth biosignals — the earlier "loss" was a wrong coder choice, not a
missing LZ stage.

*Can we beat xz on this data?* Tried, and no — and the diagnosis is precise. Per
column, `delta+ctx` ties xz on the dense columns (G_active 4.9×=4.9×) but loses on
the run-heavy ones (Sub_1 41.7× vs xz **111×**, G_intensity 6.5× vs 10.9×): xz
codes a run of N identical values as one range-coded LZ match, where our coder
pays per symbol. A better predictor (2nd-difference, fixed-order-2) and an explicit
**zero-run-length** stage both fail to close it (RLE+ctx 6.29× vs xz 8.55×) —
ctx's raw mantissa bits and per-symbol overhead can't match integrated LZ + range
coding. Beating xz *here* would mean reimplementing LZMA; the honest boundary is
that **xz wins on LZ-friendly repetitive data, we win where prediction beats LZ**
(below, and audio).

**Smooth biosignals: a better entropy coder beats xz.** PhysioNet Apnea-ECG
(8 records, 21 M samples, int16). The diagnosis came from entropy bounds: our
memoryless adaptive Rice (6.37 b/s) sat far above the residual's order-0 entropy
(5.46 b/s), while the *order-1 context* entropy — each residual's magnitude
conditioned on the previous one — is 5.03 b/s, **below xz's 5.39**. So the fix
was not LZ but a **context-adaptive entropy coder** (`compressor/ctxcoder.py`):
delta → zigzag → magnitude bucket coded by an adaptive arithmetic model selected
by the previous bucket, then raw mantissa bits.

| Apnea-ECG | gzip | zstd -19 | xz -9 | ours delta+Rice | **ours delta+ctx** |
|--|--|--|--|--|--|
| ratio | 2.16x | 2.63x | 2.99x | 2.45x | **3.16x** |

We beat `xz -9` overall by **+7.6%** — round-trip verified. The context coder uses
an **order-2** context (each residual's magnitude bucket conditioned on the
previous *two* buckets); that was chosen by measuring the residual's conditional
entropy (order-2 ≈ 4.97 b/s vs order-1's 5.14 and xz's 5.39), and it lifted the
ratio from 3.06x. Order-3 and mantissa-bit modelling were measured too and gave
too little to justify (sparser contexts / ~0.7%).

**The predictor and the entropy coder interact** (the unifying finding). The same
context coder *narrowed* the FLAC win on music (1.82x vs Rice's 1.84x), because
the LMS cascade already removes the magnitude-context it exploits, leaving a
near-memoryless residual where Rice wins. In short: **strong adaptive predictor +
Rice ≈ weak predictor + context coder.** Both ship as selectable back-ends,
chosen per type — Rice for audio, ctx for weakly-predicted signals. The honest
boundary: we win where prediction beats LZ (audio, ECG); strong LZ (xz/LZMA)
still wins on repetitive/periodic data until our own LZ path is ported to native.

**Seismic: prediction crushes LZ** (`scripts/seismic_benchmark.py`). Real broadband
seismic waveforms (integer ADC counts from IRIS — the 2010 Chile M8.8 at station
ANMO, plus a quiet microseism window; round-trip verified): high-rate, smooth,
strongly autocorrelated, with no exact-repeat structure, so LZ coders are nearly
helpless while an adaptive predictor + context coder thrives.

| segment | gzip | zstd | xz | ours |
|--|--|--|--|--|
| quake + aftershocks (416 K) | 1.57× | 1.84× | 2.29× | **6.60×** |
| quiet / microseisms (432 K) | 2.42× | 3.29× | 3.73× | **7.36×** |

We beat xz by **+97% to +188%** (2–3×) — the largest xz margin of any dataset
here. The winning configuration is the *audio* codec's fixed-2 + 16/256-tap LMS
cascade feeding `ctxcoder`: seismic is a smooth continuous waveform like music, so
those adaptive filters generalise directly (where on ECG they overshot the sharp
QRS spikes and a plain delta won). This is the sharpest point on the prediction-
friendly map — smooth, high-rate signals are exactly where prediction +
context-adaptive entropy beats general LZ coders outright.

**Floating-point: a boundary the repertoire doesn't cross** (`scripts/float_benchmark.py`).
IEEE-754 floats don't subtract meaningfully in byte space, and the test confirms
it. On real measurement float64 (power Voltage / Global_active_power), our integer
transforms *hurt* — `split(8)` gives 2.0× vs raw bytes + xz at 6.16×, because the
values repeat/jump and general LZ on the raw bytes catches that. A Gorilla-style
**XOR-delta** primitive helps only marginally, and only on a *smooth* synthetic
signal (1.36× vs 1.28×); float64 mantissas of irrational values are high-entropy,
so smooth float data is near-incompressible (~1.3×) by anything. The tempting
"detect fixed-precision → scaled int" shortcut isn't lossless (`4.216` has no exact
float64). Conclusion, kept honest: lossless float compression needs dedicated
machinery (FCM/DFCM value prediction + leading-zero/Gorilla encoding), *not* our
integer transforms — so XOR-delta was measured and **not** added (marginal, and
`split`'s proxy-selection already adapts where it helps). Raw bytes + a general
coder is the pragmatic best for measurement float64.

## Lossless video — the temporal-delta hypothesis

*The video pipeline below was developed as an ablation across a series of
exploratory scripts, now consolidated into the tested `compressor/videocodec.py`
and retired to git history; `scripts/video_ffv1_benchmark.py` reproduces the
headline FFV1 comparison.*

Most lossless video codecs (FFV1, Ut Video) are *intra-only*: each frame is
compressed independently, ignoring temporal redundancy. Hypothesis: a cheap
**temporal frame-delta** (`delta` with stride = one frame) beats intra-only coding
on static/slow content and loses on high motion (where motion compensation is
needed). Tested on standard `.y4m` sequences (luma plane), parsed with numpy — no
decoder needed. With no ffmpeg/FFV1 available,
per-frame **JPEG-XL lossless** is the intra baseline; since JXL-lossless is
*stronger* than FFV1's intra, that's a conservative stand-in. The temporal delta
is isolated by running the same intra codec on the frame residuals; our native
context coder (`ctxcoder`) also codes the residual stream. 60 frames each,
round-trip verified.

| clip (motion) | intra-JXL | temporal (best) | verdict |
|--|--|--|--|
| akiyo (static head) | 2.10 MB | **1.01 MB** (ctx) | temporal **+52%** |
| foreman (pan / medium) | 2.86 MB | 3.30 MB | intra wins −16% |
| stefan (high motion) | 3.25 MB | 3.82 MB | intra wins −18% |

The hypothesis holds exactly: frame-delta is a large win on static content and a
loss under motion — because a raw frame-delta can't track *moving* pixels, so the
residual loses the spatial structure the intra codec exploits. That is precisely
the boundary where **motion compensation** is required. One nice secondary
result: on the static clip our `ctxcoder` on the temporal residual (1.01 MB)
beats JXL-on-residual (1.23 MB) — the right entropy back-end for a near-zero
residual stream.

**Motion compensation closes the gap.** A raw
frame-delta forces a zero motion vector, so it loses where content *moves*. Block
MC — per 16×16 block, search the previous frame in a ±8 window for the min-SAD
displacement, then code (motion vector + residual) with `ctxcoder` — converts the
losses into wins/ties (60 frames, round-trip verified):

| clip | intra-JXL | frame-delta | motion-comp |
|--|--|--|--|
| akiyo (static) | 2.10 MB | 1.01 MB (+52%) | **0.95 MB (+55%)** |
| foreman (medium) | 2.86 MB | 3.30 MB (−16%) | **2.78 MB (+3%)** |
| stefan (high motion) | 3.25 MB | 3.82 MB (−18%) | **3.28 MB (−1%)** |

MC turns foreman's 16% loss into a 3% win and stefan's 18% loss into a tie —
beating intra-only JXL (itself stronger than FFV1's intra) on 2 of 3 clips. A
wider ±16 search barely moved the numbers, so the residual cost dominates: the
remaining stefan gap is occlusion / newly-revealed content that block matching
can't predict. This is the same block-search idea as our LZ match-finder, applied
across frames.

**Per-block intra/inter mode selection** removes that last loss. Each 16×16 block
picks the cheaper of INTER
(the MC residual) or INTRA (a causal **MED / LOCO-I** predictor — the JPEG-LS
median of left, above and the gradient — *within* the current frame), so
occlusion / newly-revealed blocks with no good past match fall back to intra. The
mode bit, motion vectors (inter blocks only) and residual are all ctxcoder-coded.
Reconstruction replays the intra pixels causally (intra slots start as a sentinel,
so the round-trip genuinely exercises the causal chain) — verified bit-exact.

| clip | intra-JXL | MC | MC + mode (MED) | intra blocks |
|--|--|--|--|--|
| akiyo (static) | 2.10 MB | 0.946 MB | **0.945 MB (+55%)** | 2% |
| foreman (medium) | 2.86 MB | 2.780 MB | **2.716 MB (+5%)** | 27% |
| stefan (high motion) | 3.25 MB | 3.284 MB | **3.172 MB (+2%)** | 41% |

Now **every clip beats intra-only JXL**, including high-motion stefan: MED codes
the occlusion blocks well enough that 27–41% of blocks on the motion clips choose
intra, all reusing the project's own primitives (the block search mirrors the LZ
match-finder; the residual coder is `ctxcoder`).

**Half-pixel motion vectors** add the last gain. After the integer search, each block is refined over the 9 half-pel positions
around its best integer MV (bilinear interpolation of the previous frame), keeping
the lower-SAD one; MVs are then coded in half-pel units. Real motion is rarely
integer-aligned, so this shrinks the inter residual on the moving clips:

| clip | mode int-MV | mode half-pel | vs intra-JXL |
|--|--|--|--|
| akiyo (static) | 0.945 MB | 0.934 MB | **+56%** |
| foreman (pan) | 2.716 MB | 2.600 MB | **+9%** |
| stefan (motion) | 3.172 MB | 3.054 MB | **+6%** |

Half-pel adds +1–4% on top of mode selection, and by improving inter prediction it
lets fewer blocks fall back to intra (foreman 27%→16%). The complete arc —
temporal-delta → motion compensation → per-block mode selection → MED intra →
half-pel MVs — takes **stefan from −18% to +6%** and **foreman from −16% to +9%**,
beating intra-only JXL (itself stronger than FFV1's intra) on every clip.

**A per-block SKIP mode** handles exact-static content. In a lossless codec a
block can be skipped —
*no* residual, just a mode flag — only when it is bit-identical to its prediction;
the co-located previous block (MV 0) catches static backgrounds. On akiyo's static
studio set **56% of blocks skip**, for +2.7% (→ **+57%** vs intra-only). On the
real-camera clips, sensor noise means no block is exactly static, so skip is never
chosen and costs nothing (foreman/stefan unchanged at +9% / +6%). It's a targeted
win for screen content / surveillance / animation, harmless elsewhere.

**Quarter-pixel motion vectors** refine once more: the sub-pel predictor
generalises to a single
bilinear sampler in quarter-pel units (integer / half / quarter all special cases),
and the search refines integer → half → quarter. On top of half-pel it adds
+1.5–2% — akiyo +57%→**+58%**, foreman +9%→**+10%**, stefan +6%→**+7%** vs
intra-only JXL — diminishing returns after the half-pel step, as expected. The
finished inter-frame coder (MC + quarter-pel + per-block SKIP/INTER/INTRA with MED
intra, all `ctxcoder`-coded, every frame bit-exact) takes **stefan from −18% to
+7%** and **foreman from −16% to +10%**.

**Colour planes.** Everything above is luma; the clips are 4:2:0, so U/V are
quarter-resolution chroma. Running the full pipeline independently on each plane
(60 frames, round-trip verified), the full-YUV
totals vs per-plane intra-only JXL:

| clip | Y | U | V | total | vs raw YUV |
|--|--|--|--|--|--|
| akiyo (static) | +58% | +49% | +49% | **+56%** | 7.15× (intra 3.17×) |
| foreman (pan) | +10% | +10% | +0% | **+9%** | 2.74× (intra 2.49×) |
| stefan (motion) | +7% | −4% | −2% | **+5%** | 2.22× (intra 2.11×) |

The total beats intra-only on every clip. On static content chroma compresses as
well as luma (+49%), but on the motion clips chroma is a wash or slight loss
(stefan U/V −2–4%): chroma is smooth and low-energy where intra-JXL is already
strong.

**Deriving chroma MVs from luma — tested, doesn't help here.** The textbook codec
design (one mode + one luma MV per block; chroma inherits the mode and a MV scaled
by the 4:2:0 subsampling, coding *no* chroma MV/mode)
was the obvious fix for that chroma softness. It instead **slightly regressed** vs
the independent per-plane coder (akiyo −2.7%, foreman −0.2%, stefan −0.5%; 60
frames, round-trip verified). Two reasons: joint coding gives up per-plane **SKIP**
— a chroma block is often exactly static while its luma block moves, which the
independent coder skips but the joint coder can't — and it forces a shared mode;
meanwhile `ctxcoder` already codes the small chroma MVs and mode flags so cheaply
that the removed "overhead" is negligible. So the chroma softness was never
MV/mode cost — chroma is simply smooth content intra-JXL handles well. We keep the
independent per-plane coder; the shared-MV design only pays when MV/mode coding is
expensive, which it isn't here — a reminder that codec choices are
entropy-coder-dependent.

This whole pipeline is now a **real codec**, not just benchmark scripts:
`compressor/videocodec.py` is a first-class `encode` / `decode` (and
`encode_yuv` / `decode_yuv`) that emits a `VID1` container and reconstructs frames
from it byte-exact — quarter-pel MC + per-block SKIP/INTER/INTRA (MED), residuals
and MVs via `ctxcoder`, frame 0 all-intra, depends only on numpy + ctxcoder. It's
covered by round-trip tests (all block modes, single-frame, fully-static, YUV) and
verified on real clips (akiyo 6.58×, foreman 2.30× vs raw luma, 20 frames,
bit-exact), and exposed on the CLI (`video-encode` / `video-decode` on `.y4m`).

**Real FFV1 baseline.** With a static ffmpeg (from the `imageio-ffmpeg` wheel) we
can now compare against **FFV1** — the standard intra-only lossless video codec —
instead of the JXL stand-in (`scripts/video_ffv1_benchmark.py`, full YUV, 60
frames, round-trip verified). FFV1 is intra-only, so our motion compensation wins
across the board:

| clip | raw YUV | FFV1 | ours | ours vs FFV1 |
|--|--|--|--|--|
| akiyo (static) | 9.12 MB | 2.78 MB | 1.31 MB | **+53%** |
| foreman (pan) | 9.12 MB | 3.69 MB | 3.38 MB | **+8%** |
| stefan (motion) | 9.12 MB | 4.52 MB | 4.16 MB | **+8%** |

JXL-intra came out within ~3% of FFV1 throughout, confirming it was a fair
stand-in. We beat the real specialist by exploiting the temporal redundancy it
ignores. Remaining polish: SKIP against the best MV (not just MV 0).

## Status & roadmap

A research prototype, validated end-to-end on real data across four domains
(every result round-trip verified):

- **text / byte** — trained per-type dictionary + LZ (with a validation-gated
  blob) + repeat offsets + cost-optimal parse + arithmetic coding;
- **audio** — adaptive sign-sign LMS cascade → adaptive Rice / context coder
  (beats FLAC, and xz by +59%);
- **video** — quarter-pel motion compensation + per-block SKIP/INTER/INTRA (MED
  intra) + context-adaptive residuals (beats FFV1); a real `encode`/`decode`
  (`compressor/videocodec.py`) exposed on the CLI;
- **numeric / biosignal** — per-type transform + the context-adaptive `ctxcoder`
  (beats xz on ECG; 6.27× on repetitive sensor data, beating gzip).

The whole compress/decompress hot path is **native** (C via ctypes, bit-identical
with a pure-Python fallback) — ~140× on text — so the family is fast enough to use.

The honest open frontier (full list in `TODO.md`):

- **Beat `zstd --train` on json too** — we already beat it on logs and html (via a
  512 KB blob); json is the holdout, where zstd's COVER dictionary stays ~4% ahead
  and is more byte-efficient than our blob. A better blob builder (proper COVER /
  suffix-automaton) is the lever; an earlier COVER attempt regressed, so it's still
  a real open problem.
- **More transforms** — a 2D MED/Paeth intra predictor (shared image + video), and
  proper float machinery (FCM/DFCM value prediction + Gorilla leading-zero coding)
  for the floating-point boundary the integer transforms don't cross.
- **Distribution** — an optional Rust port (single crate, `rayon` block
  parallelism) once the goal shifts from research to shipping a library.

The throughline: **predict per type, then entropy-code.** It beats the
general-purpose tools, and the domain specialists, exactly where prediction beats
LZ — and it says so honestly where LZ wins instead.
