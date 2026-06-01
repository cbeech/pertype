"""A trained, per-file-type model: a pattern dictionary plus two frequency models.

* the **main** model codes literals (0..255), dictionary references
  (256 + pattern_id), and LZ length slots (LEN_BASE + slot);
* the **distance** model codes LZ distance slots.

Both drive the arithmetic coder. Training also decides, per file type, whether
in-file LZ back-references help: it tokenizes the corpus both with and without
LZ, prices each under its own models, and keeps the cheaper mode in ``use_lz``.

Every symbol that could ever be emitted gets a baseline count, so any input is
encodable later — that is what guarantees losslessness on unseen files.
"""
from collections import Counter

from compressor.dictionary import Dictionary, mine_patterns
from compressor.freqmodel import FrequencyModel
from compressor.tokenizer import (
    MAX_DIST_SLOT, MAX_LEN_SLOT, MIN_MATCH, tokenize, value_slot,
)

MAGIC = b"CMP4"
VERSION = 4


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


def _build_candidate(samples, dictionary, use_lz):
    """Tokenize the corpus in one mode, build models, return (cost_bits, payload)."""
    n_patterns = len(dictionary.patterns)
    len_base = main_alphabet_base(n_patterns)
    main_counts, dist_counts = _baseline_counts(n_patterns, len_base)

    tokenized = [tokenize(s, dictionary, use_lz=use_lz) for s in samples]
    for tokens in tokenized:
        for tok in tokens:
            for table, sym, _ in _token_symbols(tok, len_base):
                (main_counts if table == "main" else dist_counts)[sym] += 1

    main_model = FrequencyModel.from_counts(main_counts)
    dist_model = FrequencyModel.from_counts(dist_counts)

    # Price the corpus: arithmetic cost per symbol plus raw extra bits.
    bits = 0.0
    for tokens in tokenized:
        for tok in tokens:
            for table, sym, extra in _token_symbols(tok, len_base):
                model = main_model if table == "main" else dist_model
                bits += model.cost_bits(sym) + extra
    return bits, (use_lz, main_model, dist_model)


def train(samples, type_id, max_patterns=4096, min_len=3, max_len=256):
    samples = list(samples)
    dictionary = mine_patterns(
        samples, max_patterns=max_patterns, min_len=min_len, max_len=max_len
    )

    # Pick the cheaper of {dict-only, dict+LZ} for this file type.
    candidates = [_build_candidate(samples, dictionary, use_lz) for use_lz in (False, True)]
    _, (use_lz, main_model, dist_model) = min(candidates, key=lambda c: c[0])

    return Model(
        type_id=type_id,
        dictionary=dictionary,
        main_model=main_model,
        dist_model=dist_model,
        use_lz=use_lz,
    )


class Model:
    def __init__(self, type_id, dictionary, main_model, dist_model, use_lz,
                 version=VERSION):
        self.type_id = type_id
        self.dictionary = dictionary
        self.main_model = main_model
        self.dist_model = dist_model
        self.use_lz = use_lz
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
        for blob in (
            self.dictionary.serialize(),
            self.main_model.serialize(),
            self.dist_model.serialize(),
        ):
            parts += len(blob).to_bytes(4, "big")
            parts += blob
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
        for _ in range(3):
            n = int.from_bytes(blob[pos : pos + 4], "big")
            pos += 4
            chunks.append(blob[pos : pos + n])
            pos += n

        return cls(
            type_id=type_id,
            dictionary=Dictionary.deserialize(chunks[0]),
            main_model=FrequencyModel.deserialize(chunks[1]),
            dist_model=FrequencyModel.deserialize(chunks[2]),
            use_lz=use_lz,
            version=version,
        )
