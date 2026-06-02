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
python3 scripts/make_corpus.py

# Train a model for one type
python3 -m compressor.cli train json corpus/json/train -o json.model

# Compress / decompress a single file
python3 -m compressor.cli compress some.json -m json.model -o some.json.cz
python3 -m compressor.cli decompress some.json.cz -m json.model -o roundtrip.json

# Benchmark against gzip and zstd on the held-out test set
python3 -m compressor.cli benchmark json
```

## Tests

Zero external dependencies. Run the bundled runner:

```bash
python3 -m tests.run            # all tests
python3 -m tests.run codec      # one module
```

The codec tests include property-style round-trips over random bytes, empty
input, and bytes never seen in training — proving the lossless guarantee.

## Results (held-out test sets, synthetic corpora)

Ratio = raw ÷ compressed (higher is better).

| type | gzip -9 | zstd -19 | zstd -19 +dict | **ours** | blob chosen |
|------|---------|----------|----------------|----------|-------------|
| json | 1.98x | 2.02x | 5.46x | **6.47x** ✅ | naive 32 KB |
| logs | 3.80x | 3.99x | 5.95x | **6.10x** ✅ | naive 32 KB |
| html | 2.72x | 2.70x | 10.70x | **11.28x** ✅ | coverage 64 KB |

Takeaways:

- We **beat `zstd -19 --train` (its own trained dictionary) on all three types** —
  the real apples-to-apples competitor — and beat plain gzip/zstd by 1.5–4×.
- The wins compound across the pipeline: the contiguous **LZ blob** lets matches
  reach arbitrary trained substrings (the big lift on json/html); **cost-optimal
  parsing** then squeezes the parse (it justified the blob for logs); long
  dictionary patterns, arithmetic coding, and lazy parsing each added their share.
- The **blob builder is chosen per type on a validation slice**: html packs more
  distinct structure with the COVER-style coverage builder at 64 KB, while json
  and logs do better with whole-file concatenation (long contiguous runs match
  better than fragmented coverage segments). Trying both with a naive fallback
  means the smarter builder only ever helps — html gained (10.91x → 11.28x) with
  no regression elsewhere.

Two honest costs:

- **Model size** grows with the blob and long-pattern dictionary (json ~187 KB,
  html ~367 KB). It ships once and is amortized across all files of the type, so
  the per-file numbers above are the real cost in the intended
  many-files-of-a-known-type scenario.
- **Training is slow** for LZ types (tens of seconds to a few minutes per type)
  because the cost-optimal parse runs over the blob-augmented corpus. The
  validation decision uses a shallow search depth to stay fast; only the final
  shipped model pays full depth. Compression and decompression are unaffected.

## Roadmap

We now beat `zstd +dict` on all three types. Remaining ideas, in rough order of
expected payoff:

- **Adaptive / context-modelled** probabilities (order-N) feeding the arithmetic
  coder, for text.
- **Faster training**: the validation gate now trains several blob candidates,
  so training is the slow part (tens of seconds to a few minutes per type).
  Reuse the blob's hash chains across files instead of rebuilding them, evaluate
  candidates on a subsample, and/or port the hot parse loop to Rust.

Done: trained per-type dictionary (frequency × savings, long patterns admitted),
LZ back-references with a contiguous trained blob, two blob builders (naive and
COVER-style coverage) chosen per type on a validation slice, lazy parsing,
cost-optimal parsing, and arithmetic coding.
