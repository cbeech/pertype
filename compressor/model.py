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
    MAX_DIST_SLOT, MAX_LEN_SLOT, MIN_MATCH, tokenize, value_slot,
)

MAGIC = b"CMP5"
VERSION = 5

# Size cap for the trained LZ blob (contiguous corpus content).
BLOB_CAP = 1 << 15  # 32 KiB


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


def _build_blob(samples, cap=BLOB_CAP):
    """A contiguous slice of corpus content for LZ to reference into."""
    blob = bytearray()
    for s in samples:
        if len(blob) >= cap:
            break
        blob += s
    return bytes(blob[:cap])


def _artifacts(samples, use_lz, max_patterns, min_len, max_len):
    """Build dictionary, blob, and frequency models for one mode."""
    dictionary = mine_patterns(
        samples, max_patterns=max_patterns, min_len=min_len, max_len=max_len
    )
    blob = _build_blob(samples) if use_lz else b""
    len_base = main_alphabet_base(len(dictionary.patterns))
    main_counts, dist_counts = _baseline_counts(len(dictionary.patterns), len_base)

    for s in samples:
        for tok in tokenize(s, dictionary, use_lz=use_lz, prefix=blob):
            for table, sym, _ in _token_symbols(tok, len_base):
                (main_counts if table == "main" else dist_counts)[sym] += 1

    return (
        dictionary,
        blob,
        FrequencyModel.from_counts(main_counts),
        FrequencyModel.from_counts(dist_counts),
    )


def _price(samples, dictionary, blob, main_model, dist_model, use_lz):
    """Total arithmetic-coded bits for ``samples`` under the given artifacts."""
    len_base = main_alphabet_base(len(dictionary.patterns))
    bits = 0.0
    for s in samples:
        for tok in tokenize(s, dictionary, use_lz=use_lz, prefix=blob):
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

    best_cost, chosen = None, False
    for use_lz in (False, True):
        d, b, mm, dm = _artifacts(fit, use_lz, max_patterns, min_len, max_len)
        cost = _price(val, d, b, mm, dm, use_lz)
        if best_cost is None or cost < best_cost:
            best_cost, chosen = cost, use_lz

    # Rebuild final artifacts on all samples in the chosen mode.
    dictionary, blob, main_model, dist_model = _artifacts(
        samples, chosen, max_patterns, min_len, max_len
    )
    return Model(
        type_id=type_id,
        dictionary=dictionary,
        blob=blob,
        main_model=main_model,
        dist_model=dist_model,
        use_lz=chosen,
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
