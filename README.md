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

On top of the cross-file dictionary, the codec also uses **in-file LZ77
back-references** (repeated lines/rows within a single file). Training decides
*per file type* whether LZ actually pays off and records the choice in the model
(`use_lz`), so a type already covered by the dictionary never pays for it.

The twist that beats general-purpose tools: the model is **trained per file type
and shipped separately**, not embedded in every compressed file the way gzip is.
That cost is paid once and amortized across many files. The honest win-scenario
is therefore **many smallish files of a known type** (API responses, log lines,
HTML pages).

## How it works

```
train(corpus)                         compress(file, model)
  mine common patterns  ─┐              tokenize: longest of {dict match,
  price dict-only vs LZ  ├─ model         in-file LZ match} else literal
  build Huffman tables  ─┘                └─ Huffman-encode the token stream
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

| type | gzip -9 | zstd -19 | zstd -19 +dict | **ours** | LZ mode |
|------|---------|----------|----------------|----------|---------|
| json | 1.98x | 2.02x | 5.46x | **5.36x** | off |
| logs | 3.80x | 3.99x | 5.95x | **5.41x** | off |
| html | 2.72x | 2.70x | 10.70x | **6.94x** | on |

Takeaways:

- We **beat plain gzip/zstd by 1.4–2.6×** on every type — that's the trained
  per-type dictionary doing its job.
- We're **within ~2% of zstd's own trained dictionary on JSON** (the real
  apples-to-apples competitor), close on logs, and behind it on html.
- In-file LZ is **learned per type**: it lifts html while json and logs correctly
  opt out, so adding it never regresses a type.
- Switching Huffman → **arithmetic coding** gained ~1–2.5% across types. The gain
  is small because Huffman was already near-entropy for these alphabets; the
  remaining gap to `zstd +dict` (especially on html) is **parse and dictionary
  quality**, not the entropy coder — zstd uses an optimal parse and the COVER
  dictionary trainer, where we use a greedy parse and a greedy substring miner.

Model size is reported separately by the benchmark because it ships once; the
per-file numbers above are the amortized cost.

## Roadmap

The benchmarks show the biggest lever now is **dictionary quality**, not parsing
or entropy coding:

- A better **dictionary trainer** (e.g. suffix-automaton or COVER-style) in place
  of the greedy substring miner — the largest remaining win, especially on html,
  where zstd's 112 KB COVER-trained dictionary captures far more than ours.
- **Cost-optimal parsing** (full shortest-path over the token graph) beyond the
  one-byte lazy lookahead already implemented.
- **Adaptive / context-modelled** probabilities (order-N) feeding the arithmetic
  coder, for text.
- Port the hot path to Rust for production speed.

Done: trained per-type dictionary, in-file LZ back-references with a learned
per-type on/off decision, lazy match parsing, and an arithmetic entropy coder.
```
