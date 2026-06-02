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
  mine patterns + blob  ─┐              cost-optimal parse (DP over the token
  price dict-only vs     ├─ model         graph) using the model's bit costs
    LZ+blob on val set  ─┘                └─ arithmetic-code the token stream
  pick cheaper mode                          └─ container = header + bitstream
```

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
| json | 5.70x | 6.18x | **9.42x** | 7.32x |
| logs | 7.40x | 7.76x | **14.06x** | 9.19x |
| html | 3.86x | 3.98x | **7.08x** | 5.57x |

On real, heterogeneous files we **beat plain gzip / zstd -19 by 18–40%** (the core
thesis — a per-type trained model beats general compressors — holds), but
**`zstd -19 --train` clearly beats us** (we reach 65–79% of its ratio). zstd's
edge on hard data comes from machinery we don't have: a COVER-trained dictionary,
repeat-offset modeling, a deeper optimal parse, and FSE coding. The shipped model
is also large here (html ~980 KB), so it only amortizes over many files.

### Synthetic corpora — where we win (but it's partly overfit)

| type | gzip -9 | zstd -19 | zstd -19 +dict | **ours** | blob chosen |
|------|---------|----------|----------------|----------|-------------|
| json | 1.98x | 2.02x | 5.46x | **6.47x** ✅ | naive 32 KB |
| logs | 3.80x | 3.99x | 5.95x | **6.10x** ✅ | naive 32 KB |
| html | 2.72x | 2.70x | 10.70x | **11.28x** ✅ | coverage 64 KB |

On the synthetic corpus we beat `zstd +dict` on all three types — but the
synthetic files are highly homogeneous, which flatters our approach. The
real-world numbers above are the truer measure; the gap between the two tables is
itself the lesson: **validate on real data.**

Takeaways:

- The **core thesis holds on real data**: a per-type trained model beats
  general-purpose gzip/zstd by 18–40%. The pipeline that gets there — trained
  dictionary, contiguous LZ blob, cost-optimal parse, arithmetic coding — each
  step compounds.
- But **a mature engine's trained-dictionary mode (zstd --train) still wins on
  real, heterogeneous data.** Our synthetic-corpus wins were partly overfit to
  homogeneous files; testing on real files corrected the picture.
- The **blob builder is chosen per type on a validation slice** (naive vs
  COVER-style coverage, several sizes), so the smarter builder only helps where it
  helps and never regresses a type.

Honest costs:

- **Model size** grows with the blob and dictionary (real html ~980 KB). It ships
  once and amortizes across many files, but on heterogeneous data that amortizes
  less well.
- **Training is slow** and **cost-optimal parsing doesn't scale to large files**
  in pure Python (real html — ~16 KB/file — took minutes). Compression and
  decompression of small files are fine; large-file throughput needs work.

## Roadmap

The real-world gap to `zstd --train` is the thing to close. In rough order of
expected payoff:

- **Repeat-offset modeling** (reuse recent match distances cheaply) — a big part
  of zstd's edge, and a clean addition to the token/cost model.
- A genuinely better **dictionary trainer for heterogeneous data** (proper COVER
  / suffix-automaton selection) — ours is tuned to homogeneous corpora.
- **Faster parse** (reuse blob hash chains across files, Rust hot loop) so
  cost-optimal depth is affordable on large files.
- **Adaptive / context-modelled** literal coding — tried (order-1 with per-file
  adaptation); measured ~0.5% on these corpora because residual literals are
  near-random after dict+LZ, so it was shelved. Likely worth more on
  natural-language text.

Done: trained per-type dictionary (frequency × savings, long patterns admitted),
LZ back-references with a contiguous trained blob, two blob builders (naive and
COVER-style coverage) chosen per type on a validation slice, lazy parsing,
cost-optimal parsing, and arithmetic coding. Validated on both synthetic and
real-world corpora.
