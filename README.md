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

Beyond text, a per-type **transform stage** (an auto-selected reversible
decorrelation) extends the same idea to numeric and media data. Measured on real
data across three domains, the headline:

- **Text** — beat plain gzip/zstd by 29–62%; ~parity with `zstd --train`.
- **Raw images** (Canon CR2 Bayer) — **statistical parity with JPEG XL**, the
  state-of-the-art lossless image codec; beat Canon, zstd, gzip, PNG outright.
- **Audio** (16-bit PCM) — the generic codec loses to FLAC, so we built a
  dedicated adaptive-filter audio codec that **beats FLAC** (+7.4% mean, 9/10
  tracks).

The unifying lesson (see the cross-domain sections): a cheap per-type transform
closes the gap to a domain specialist by as much as that specialist's modeling
exceeds simple decorrelation.

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
| `compressor/native.py` + `_native/audio.c` | C hot loops (ctypes), auto-built, with Python fallback |
| `compressor/benchmark.py` | comparison vs gzip / zstd / zstd-trained-dict |
| `compressor/cli.py` | `train` / `compress` / `decompress` / `benchmark` |

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

## Dependencies

- **Core text/byte compressor and tests: zero external dependencies** (Python 3
  stdlib only — `codec.py`, `model.py`, `tokenizer.py`, etc.).
- **`compressor/audiocodec.py`** (the dedicated audio codec): needs `numpy`.
- **CLI benchmark** (`compressor.cli benchmark`): the `gzip` and `zstd` command-line
  tools.
- **Cross-domain benchmark scripts** need the libraries in the table above —
  install with: `pip install pillow rawpy numpy imagecodecs soundfile`
  (`imagecodecs` bundles libjxl for the JPEG XL comparison; `soundfile` bundles
  libsndfile for FLAC). These are *only* for the optional benchmarks, never the
  codec itself.

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
| json | 5.70x | 6.18x | **9.42x** | 7.98x |
| logs | 7.40x | 7.76x | **14.06x** | 10.87x |
| html | 3.86x | 3.98x | **7.08x** | 6.46x |

On real, heterogeneous files we **beat plain gzip / zstd -19 by 29–62%** (the core
thesis — a per-type trained model beats general compressors — holds well). We do
**not** beat `zstd -19 --train`, but reach **77–91% of it** (closest on html).
Scaling the blob toward zstd's ~110 KB dictionary size and deepening the search
got us here; pushing further hits sharp diminishing returns (a 512 KB blob reached
only 8.77x on json) because **zstd is far more byte-efficient** — its 110 KB
COVER-trained dictionary beats our much larger blob. The shipped model is large
(real html ~1.1 MB), so it only amortizes over many files.

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

**Ours beats FLAC on 9/10 tracks, mean +7.4%** (up to +22%). The third (512-tap)
LMS stage added +1.5 points of margin over the prior two-stage cascade (measured
on 12 tracks, better on 11/12). Caveats: vs
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
`scripts/ecg_*`). This sharpened the thesis — producing one honest loss and one
genuine win over `xz`.

**Repetitive data wants LZ, which we don't have.** UCI household power
(2.05 M rows × 7 sensor columns, exact int32 milli-units): **51 % of deltas are
exactly zero** — long constant runs (appliances off, coarse quantisation). That
is RLE/LZ territory, not predictor territory, and delta barely helps even the
general tools (zstd 7.50→7.51). We lose badly.

| household power | gzip | zstd -19 | xz -9 | delta+xz | ours (predict+Rice) |
|--|--|--|--|--|--|
| ratio | 6.15x | 7.50x | **8.56x** | **8.75x** | 2.90x |

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

## Roadmap

The real-world gap to `zstd --train` is the thing to close. In rough order of
expected payoff:

- **More transforms.** The transform stage is the highest-leverage idea in the
  codebase — it turns "no domain modeling" into *automatic per-type* domain
  modeling, which general-purpose tools don't do. Add 2D-aware predictors
  (MED/Paeth with a learned row stride), RLE for the long zero-runs decorrelation
  produces, and de-interleaving for stereo audio / columnar data.
- A genuinely better **dictionary trainer for heterogeneous data** (proper COVER
  / suffix-automaton selection) — the largest remaining lever on real *text*.
- **Faster parse** (reuse blob hash chains across files, Rust hot loop) so
  cost-optimal depth is affordable on large files.

Done: trained per-type dictionary (frequency × savings, long patterns admitted),
LZ back-references with a contiguous trained blob, two blob builders (naive and
COVER-style coverage) chosen per type on a validation slice, lazy parsing,
cost-optimal parsing, repeat-offset modeling, arithmetic coding, a per-type
reversible transform stage (delta/split decorrelation), and a dedicated
adaptive-filter audio codec that beats FLAC. Validated on synthetic text,
real-world text, raw images (parity with JPEG XL), and lossless audio (beats
FLAC) — every result round-trip verified on real data.
