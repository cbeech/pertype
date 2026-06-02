"""A trained, per-file-type model: a pattern dictionary plus two frequency models.

* the **main** model codes literals (0..255), dictionary references
  (256 + pattern_id), and LZ length slots (LEN_BASE + slot);
* the **distance** model codes LZ distance slots.

Both drive the arithmetic coder. When LZ is used, a trained **blob** (a
contiguous slice of corpus content) is prepended to each file's history so LZ
matches can reach into arbitrary substrings of it — the way zstd uses a
dictionary. Atomic dictionary patterns remain (cheap one-symbol references), so
types that don't benefit from LZ keep their efficient encoding untouched.

Training decides, per file type, whether LZ (with the blob) helps. To stop the
blob from overfitting that decision, the choice is made on a held-out
**validation slice**: artifacts are built on the fit slice, both modes are priced
on the validation slice, and the cheaper wins. Final artifacts are then rebuilt
on all samples.

Every symbol that could ever be emitted gets a baseline count, so any input is
encodable later — that is what guarantees losslessness on unseen files.
"""
from collections import Counter

from compressor.dictionary import Dictionary, mine_patterns
from compressor.freqmodel import FrequencyModel
from compressor.tokenizer import (
    MAX_CHAIN, MAX_DIST_SLOT, MAX_LEN_SLOT, MIN_MATCH,
    tokenize, tokenize_optimal, value_slot,
)

MAGIC = b"CMP5"
VERSION = 5

# Trained LZ blob: a contiguous slice of representative corpus content. Built by
# COVER-style coverage selection (see _build_blob).
BLOB_CAP = 1 << 15      # 32 KiB
BLOB_DMER = 8           # coverage sub-unit length
BLOB_SEGMENT = 2048     # candidate segment length (large: preserves contiguity
                        # so files can still LZ-match long runs)
BLOB_STRIDE = 512       # spacing between candidate segments

# Shallow hash-chain depth for the use_lz validation *decision*, which is just a
# binary choice and robust to search depth. The final shipped model is built with
# the full depth (tokenizer.MAX_CHAIN) so its frequencies match compression.
DECISION_CHAIN = 16


def main_alphabet_base(n_patterns):
    """First main symbol used for LZ length slots."""
    return 256 + n_patterns


def _token_symbols(tok, len_base):
    """Yield (table, symbol, extra_bits) contributions for one token.

    table is "main" or "dist"; extra_bits is the count of raw bits that follow
    the coded symbol (0 for literals/dict refs).
    """
    kind = tok[0]
    if kind == "lit":
        yield ("main", tok[1], 0)
    elif kind == "dict":
        yield ("main", 256 + tok[1], 0)
    else:  # match
        length, distance = tok[1], tok[2]
        lslot, _ = value_slot(length - MIN_MATCH + 1)
        yield ("main", len_base + lslot, lslot)
        dslot, _ = value_slot(distance)
        yield ("dist", dslot, dslot)


def _baseline_counts(n_patterns, len_base):
    main = Counter()
    dist = Counter()
    for b in range(256):
        main[b] = 1
    for pid in range(n_patterns):
        main[256 + pid] = 1
    for slot in range(MAX_LEN_SLOT + 1):
        main[len_base + slot] = 1
    for slot in range(MAX_DIST_SLOT + 1):
        dist[slot] = 1
    return main, dist


def _build_blob(samples, cap=BLOB_CAP, d=BLOB_DMER, seg=BLOB_SEGMENT,
                stride=BLOB_STRIDE, max_bytes=1_000_000):
    """Build the LZ blob by COVER-style coverage selection.

    Candidate segments are scored by how much *popular content* they cover
    (summed frequencies of their ``d``-byte sub-units). The highest scorer is
    taken, its sub-units are retired (zeroed) so later picks must bring new
    content, and we repeat until the cap is hit. This is the right move for a
    contiguous blob — any substring is referenceable, so retiring covered content
    is sound (unlike for atomic patterns). Selected segments are concatenated
    with the most valuable **nearest the data** (end of the blob), so the
    most-matched content gets the smallest, cheapest distances.
    """
    src = b"".join(samples)
    if len(src) > max_bytes:
        src = src[:max_bytes]
    n = len(src)
    if n <= cap:
        return src

    dmer_freq = Counter()
    for i in range(n - d + 1):
        dmer_freq[src[i : i + d]] += 1

    def seg_dmers(start, length):
        return [src[j : j + d] for j in range(start, start + length - d + 1)]

    candidates = []
    for start in range(0, n - d + 1, stride):
        length = min(seg, n - start)
        if length < d:
            continue
        score = sum(dmer_freq[dm] for dm in seg_dmers(start, length))
        candidates.append((score, start, length))
    candidates.sort(key=lambda x: -x[0])

    selected = []          # high value first
    total = 0
    for _, start, length in candidates:
        if total >= cap:
            break
        dms = seg_dmers(start, length)
        if sum(dmer_freq[dm] for dm in dms) <= 0:
            continue       # already covered by earlier picks
        piece = src[start : start + length]
        if total + len(piece) > cap:
            piece = piece[: cap - total]
        selected.append(piece)
        total += len(piece)
        for dm in dms:
            dmer_freq[dm] = 0

    # Most valuable (first selected) nearest the data → reverse before joining.
    return b"".join(reversed(selected))


def _build_blob_naive(samples, cap=BLOB_CAP):
    """Whole training files concatenated until the cap — preserves long
    contiguous runs, which sometimes match held-out files better than the
    coverage-selected blob (e.g. short-record JSON)."""
    blob = bytearray()
    for s in samples:
        if len(blob) >= cap:
            break
        blob += s
    return bytes(blob[:cap])


def _blob_for(spec, samples):
    """Build the blob for a spec ``(method, cap)``. method: none/cover/naive."""
    method, cap = spec
    if method == "none":
        return b""
    if method == "naive":
        return _build_blob_naive(samples, cap)
    return _build_blob(samples, cap=cap)


# Blob strategies tried per type during the validation decision; cheapest wins.
BLOB_SPECS = (
    ("none", 0),
    ("naive", 1 << 15),
    ("cover", 1 << 14),
    ("cover", 1 << 15),
    ("cover", 1 << 16),
)


def _models_from_tokenized(tokenized, n_patterns, len_base):
    main_counts, dist_counts = _baseline_counts(n_patterns, len_base)
    for tokens in tokenized:
        for tok in tokens:
            for table, sym, _ in _token_symbols(tok, len_base):
                (main_counts if table == "main" else dist_counts)[sym] += 1
    return FrequencyModel.from_counts(main_counts), FrequencyModel.from_counts(dist_counts)


def token_costs(main_model, dist_model, len_base):
    """Bit-cost callables ``(lit, dict, match)`` for the optimal parser."""

    def lit_cost(byte):
        return main_model.cost_bits(byte)

    def dict_cost(pid):
        return main_model.cost_bits(256 + pid)

    def match_cost(length, distance):
        lslot, _ = value_slot(length - MIN_MATCH + 1)
        dslot, _ = value_slot(distance)
        return (
            main_model.cost_bits(len_base + lslot) + lslot
            + dist_model.cost_bits(dslot) + dslot
        )

    return lit_cost, dict_cost, match_cost


def _parse(samples, dictionary, blob, use_lz, costs=None, max_chain=MAX_CHAIN):
    """Tokenize the corpus. LZ types use the cost-optimal parser; dict-only types
    use greedy longest-match."""
    if not use_lz:
        return [tokenize(s, dictionary, use_lz=False) for s in samples]
    return [
        tokenize_optimal(s, dictionary, costs, prefix=blob, max_chain=max_chain)
        for s in samples
    ]


def _artifacts(samples, blob, max_patterns, min_len, max_len, max_chain=MAX_CHAIN):
    """Build dictionary and frequency models for a given (prebuilt) blob.

    use_lz is implied by a non-empty blob. For LZ types a provisional model from
    a fast lazy parse supplies the costs for one cost-optimal re-parse, from
    which the final model is built.
    """
    use_lz = len(blob) > 0
    dictionary = mine_patterns(
        samples, max_patterns=max_patterns, min_len=min_len, max_len=max_len
    )
    n_patterns = len(dictionary.patterns)
    len_base = main_alphabet_base(n_patterns)

    if use_lz:
        # Bootstrap costs from a fast lazy parse; the blob makes these prices
        # match what the cost-optimal re-parse actually sees.
        provisional = [
            tokenize(s, dictionary, use_lz=True, prefix=blob, max_chain=DECISION_CHAIN)
            for s in samples
        ]
        pm, pd = _models_from_tokenized(provisional, n_patterns, len_base)
        costs = token_costs(pm, pd, len_base)
        tokenized = _parse(samples, dictionary, blob, True, costs, max_chain)
    else:
        tokenized = _parse(samples, dictionary, blob, False)

    main_model, dist_model = _models_from_tokenized(tokenized, n_patterns, len_base)
    return dictionary, main_model, dist_model


def _price(samples, dictionary, blob, main_model, dist_model, max_chain=MAX_CHAIN):
    """Total arithmetic-coded bits for ``samples`` under the given artifacts,
    parsed as compression will parse them (at the given search depth)."""
    use_lz = len(blob) > 0
    len_base = main_alphabet_base(len(dictionary.patterns))
    costs = token_costs(main_model, dist_model, len_base) if use_lz else None
    bits = 0.0
    for tokens in _parse(samples, dictionary, blob, use_lz, costs, max_chain):
        for tok in tokens:
            for table, sym, extra in _token_symbols(tok, len_base):
                model = main_model if table == "main" else dist_model
                bits += model.cost_bits(sym) + extra
    return bits


def train(samples, type_id, max_patterns=4096, min_len=3, max_len=256):
    samples = list(samples)

    # Decide use_lz on a held-out validation slice so the blob can't overfit it.
    if len(samples) >= 5:
        cut = max(1, len(samples) * 4 // 5)
        fit, val = samples[:cut], samples[cut:]
    else:
        fit = val = samples

    # Try each blob strategy (none / naive / coverage at several sizes) and keep
    # whichever is cheapest on the validation slice — a shallow search depth keeps
    # the decision fast. Each type ends up with the blob that suits it, so the
    # smarter coverage builder can win where it helps without regressing others.
    best_cost, best_spec = None, ("none", 0)
    for spec in BLOB_SPECS:
        blob = _blob_for(spec, fit)
        d, mm, dm = _artifacts(fit, blob, max_patterns, min_len, max_len, DECISION_CHAIN)
        cost = _price(val, d, blob, mm, dm, DECISION_CHAIN)
        if best_cost is None or cost < best_cost:
            best_cost, best_spec = cost, spec

    # Rebuild final artifacts on all samples with the chosen blob (full depth).
    blob = _blob_for(best_spec, samples)
    dictionary, main_model, dist_model = _artifacts(
        samples, blob, max_patterns, min_len, max_len, MAX_CHAIN
    )
    return Model(
        type_id=type_id,
        dictionary=dictionary,
        blob=blob,
        main_model=main_model,
        dist_model=dist_model,
        use_lz=len(blob) > 0,
    )


class Model:
    def __init__(self, type_id, dictionary, main_model, dist_model, use_lz,
                 blob=b"", version=VERSION):
        self.type_id = type_id
        self.dictionary = dictionary
        self.main_model = main_model
        self.dist_model = dist_model
        self.use_lz = use_lz
        self.blob = blob
        self.version = version

    @property
    def len_base(self):
        return main_alphabet_base(len(self.dictionary.patterns))

    def costs(self):
        """Token bit-cost callables for the cost-optimal parser."""
        return token_costs(self.main_model, self.dist_model, self.len_base)

    def save(self):
        parts = bytearray()
        parts += MAGIC
        parts += self.version.to_bytes(2, "big")
        parts += bytes([1 if self.use_lz else 0])
        tid = self.type_id.encode("utf-8")
        parts += bytes([len(tid)])
        parts += tid
        for chunk in (
            self.dictionary.serialize(),
            self.main_model.serialize(),
            self.dist_model.serialize(),
            self.blob,
        ):
            parts += len(chunk).to_bytes(4, "big")
            parts += chunk
        return bytes(parts)

    @classmethod
    def load(cls, blob):
        if blob[:4] != MAGIC:
            raise ValueError("not a compressor model file")
        pos = 4
        version = int.from_bytes(blob[pos : pos + 2], "big")
        pos += 2
        use_lz = bool(blob[pos])
        pos += 1
        tid_len = blob[pos]
        pos += 1
        type_id = blob[pos : pos + tid_len].decode("utf-8")
        pos += tid_len

        chunks = []
        for _ in range(4):
            n = int.from_bytes(blob[pos : pos + 4], "big")
            pos += 4
            chunks.append(blob[pos : pos + n])
            pos += n

        return cls(
            type_id=type_id,
            dictionary=Dictionary.deserialize(chunks[0]),
            main_model=FrequencyModel.deserialize(chunks[1]),
            dist_model=FrequencyModel.deserialize(chunks[2]),
            blob=chunks[3],
            use_lz=use_lz,
            version=version,
        )
