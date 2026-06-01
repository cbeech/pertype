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
When LZ is enabled, training prepends a learned **blob** (a contiguous slice of
corpus content) to each file's history, so matches can reach into arbitrary
substrings of trained content — the way zstd uses a dictionary — as well as
in-file repetition. Training decides *per file type* whether LZ+blob pays off,
**deciding on a held-out validation slice** so the blob can't overfit the choice;
the result is recorded in the model (`use_lz`). A type already covered by the
atomic dictionary (e.g. logs) keeps its efficient dict-only encoding.

The twist that beats general-purpose tools: the model is **trained per file type
and shipped separately**, not embedded in every compressed file the way gzip is.
That cost is paid once and amortized across many files. The honest win-scenario
is therefore **many smallish files of a known type** (API responses, log lines,
HTML pages).

## How it works

```
train(corpus)                         compress(file, model)
  mine patterns + blob  ─┐              tokenize: longest of {dict match,
  price dict-only vs     ├─ model         LZ match into blob/history} else literal
    LZ+blob on val set  ─┘                └─ arithmetic-code the token stream
  pick cheaper mode                          └─ container = header + bitstream
```

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

| type | gzip -9 | zstd -19 | zstd -19 +dict | **ours** | LZ+blob |
|------|---------|----------|----------------|----------|---------|
| json | 1.98x | 2.02x | 5.46x | **6.03x** ✅ | on |
| logs | 3.80x | 3.99x | 5.95x | **5.63x** | off |
| html | 2.72x | 2.70x | 10.70x | **10.16x** | on |

Takeaways:

- We **beat plain gzip/zstd by 1.4–3.7×** on every type — that's the trained
  per-type dictionary doing its job.
- On **JSON we now beat zstd's own trained dictionary** (6.03x vs 5.46x), and on
  **html we're within ~5%** (10.16x vs 10.70x) — both thanks to the contiguous
  LZ blob letting matches reach arbitrary trained substrings.
- The blob is **learned per type on a validation slice**: json and html adopt it
  (genuine wins), logs declines it and keeps dict-only — so it never regresses a
  type even though it's a big win for two of them.
- Earlier steps compounded: long dictionary patterns lifted logs most; arithmetic
  coding added ~1–2.5%; lazy parsing helped html.

The flip side: the blob and long-pattern dictionary grow the **model** (json
~187 KB, html ~367 KB). It ships once and is amortized across all files of the
type, so the per-file numbers above are the real cost in the intended
many-files-of-a-known-type scenario.

## Roadmap

We now beat or nearly match `zstd +dict` on all three types. Remaining ideas, in
rough order of expected payoff:

- **Cost-optimal parsing** (shortest-path over the token graph) beyond the
  one-byte lazy lookahead — should help html close the last ~5%.
- A smarter **blob builder** (representative-segment selection à la zstd COVER,
  most-useful content nearest the data) rather than a raw corpus slice; may also
  let logs benefit.
- **Adaptive / context-modelled** probabilities (order-N) feeding the arithmetic
  coder, for text.
- Port the hot path to Rust for production speed.

Done: trained per-type dictionary (frequency × savings, long patterns admitted),
LZ back-references with a contiguous trained blob, a validation-gated per-type
LZ/blob decision, lazy match parsing, and an arithmetic entropy coder.
