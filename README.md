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
the cheapest token.

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

## Tests

Zero external dependencies. Run the bundled runner:

```bash
python3 -m tests.run            # all tests
python3 -m tests.run codec      # one module
```

The codec tests include property-style round-trips over random bytes, empty
input, and bytes never seen in training — proving the lossless guarantee.

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

(CR2 reference: Canon's own full-frame lossless ≈ 1.6–1.75x. Raw sensor noise is
near-incompressible — these ratios are close to the information-theoretic floor.)

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

## Audio domain — where the specialist wins

Lossless audio (16-bit PCM, real music) decoded via libsndfile; **FLAC** is the
purpose-built baseline (`scripts/audio_benchmark.py`). The gate again
auto-selects `delta(4)+split(2)`.

| gzip -9 | zstd -19 | **ours** | FLAC |
|---------|----------|----------|------|
| 1.03x | 1.03x | **1.16x** | **1.59x** |

The transform helps (we beat gzip/zstd, which are near-helpless on PCM), **but
FLAC wins decisively.** Unlike Bayer mosaics, audio rewards *adaptive high-order
linear prediction* (LPC): a fixed stride-delta is only a 1st-order predictor, and
fixed orders ≥3 actually get *worse* (they amplify noise). No simple transform
reaches FLAC.

This completes the cross-domain map and the unifying principle: **a per-type
reversible transform closes the gap to a domain specialist by as much as that
specialist's modeling exceeds simple decorrelation** — large for Bayer (1st-order
structure → JPEG-XL parity), small for audio (adaptive LPC → FLAC stays ahead).
It tells you exactly when this architecture is worth deploying: structured data
whose redundancy a cheap reversible transform can expose.

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
cost-optimal parsing, repeat-offset modeling, arithmetic coding, and a per-type
reversible transform stage (delta/split decorrelation). Validated on synthetic,
real-world, and image/raw corpora.
