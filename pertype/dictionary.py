"""Pattern dictionary: training (mining) and longest-match lookup.

The dictionary is an ordered list of byte patterns. A pattern's index is its
id; the tokenizer references it by that id.

``mine_patterns`` ranks candidate substrings by **frequency × bytes-saved** and
keeps the top ones. Two refinements over a naive count: it admits *long* patterns
(up to ``max_len``), so identical boilerplate (HTML heads, footers) is captured
whole instead of chopped into fixed-size pieces; and it generates those long
candidates only where their leading "dmer" recurs, which keeps the candidate set
(and memory) bounded by pruning unique content.

A coverage/dedup scheme à la zstd's COVER was tried and removed: COVER assumes a
contiguous dictionary blob where any substring is referenceable, so retiring
covered content is harmless. Here patterns are *atomic* (matched as whole units),
so retiring the sub-units of one long pattern wrongly suppressed the short,
broadly useful patterns — it made every type worse. Frequency × savings, which
lets a short high-frequency key and a long boilerplate block coexist, wins.
"""
from collections import Counter

# Approximate cost (in bytes) of referencing a pattern; a pattern must be longer
# than this to be worth keeping.
_REFERENCE_COST = 2

# Sub-unit length used only to prune unique long content from the candidate set.
_DMER = 8

# Candidate lengths span short tokens up to long boilerplate blocks.
_DEFAULT_LENGTHS = (3, 4, 5, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256)


def mine_patterns(
    samples,
    max_patterns=4096,
    min_len=3,
    max_len=256,
    max_mining_bytes=1_000_000,
    dmer=_DMER,
):
    """Learn a :class:`Dictionary` from an iterable of byte samples."""
    blob = b"".join(samples)
    if len(blob) > max_mining_bytes:
        blob = blob[:max_mining_bytes]
    n = len(blob)

    d = min(dmer, max(1, min_len))
    lengths = [l for l in _DEFAULT_LENGTHS if min_len <= l <= max_len]
    if not lengths:
        lengths = [min_len]

    # Dmer frequencies, used only to prune unique long candidates.
    dmer_freq = Counter()
    for i in range(n - d + 1):
        dmer_freq[blob[i : i + d]] += 1

    # Count candidate substrings. Long candidates (>= d) are only taken where the
    # leading dmer repeats, so unique content does not explode the candidate set;
    # short candidates are always counted.
    counts = Counter()
    for length in lengths:
        if length >= d:
            for i in range(n - length + 1):
                if dmer_freq[blob[i : i + d]] >= 2:
                    counts[blob[i : i + length]] += 1
        else:
            for i in range(n - length + 1):
                counts[blob[i : i + length]] += 1

    # Score = occurrences * bytes saved per use. Keep only patterns seen more
    # than once and long enough to pay for a reference.
    scored = []
    for pattern, freq in counts.items():
        if freq < 2:
            continue
        saving = len(pattern) - _REFERENCE_COST
        if saving <= 0:
            continue
        scored.append((freq * saving, pattern))
    scored.sort(key=lambda x: (-x[0], -len(x[1]), x[1]))

    patterns = [pattern for _, pattern in scored[:max_patterns]]
    return Dictionary(patterns)


class Dictionary:
    def __init__(self, patterns):
        self.patterns = list(patterns)
        # Index by 2-byte prefix -> [(pattern_bytes, id)] sorted longest-first,
        # so the first prefix hit during a scan is the longest match.
        self._index = {}
        for pid, pattern in enumerate(self.patterns):
            key = bytes(pattern[:2])
            self._index.setdefault(key, []).append((pattern, pid))
        for bucket in self._index.values():
            bucket.sort(key=lambda x: -len(x[0]))

    def flat_index(self):
        """Flat arrays of the 2-byte-prefix index for the native matcher (cached):
        ``(pat_data, pat_off, bucket_off, bucket_pids)`` — pattern bytes
        concatenated with offsets, and a CSR over 2^16 keys of pattern ids in the
        same longest-first order as ``self._index``."""
        flat = getattr(self, "_flat", None)
        if flat is None:
            import numpy as np
            pats = self.patterns
            pat_off = np.zeros(len(pats) + 1, dtype=np.int32)
            for i, p in enumerate(pats):
                pat_off[i + 1] = pat_off[i] + len(p)
            joined = b"".join(bytes(p) for p in pats)
            pat_data = np.frombuffer(joined, dtype=np.uint8).copy()
            bucket_off = np.zeros(65537, dtype=np.int32)
            lists = [None] * 65536
            for keyb, bucket in self._index.items():
                lists[(keyb[0] << 8) | keyb[1]] = [pid for _pat, pid in bucket]
            pids = []
            for k in range(65536):
                if lists[k]:
                    pids.extend(lists[k])
                bucket_off[k + 1] = len(pids)
            bucket_pids = np.asarray(pids, dtype=np.int32)
            flat = self._flat = (pat_data, pat_off, bucket_off, bucket_pids)
        return flat

    def match(self, data, pos, min_match):
        """Longest pattern that is a prefix of ``data[pos:]``.

        Returns ``(pattern_id, length)`` or ``None``.
        """
        key = bytes(data[pos : pos + 2])
        bucket = self._index.get(key)
        if not bucket:
            return None
        for pattern, pid in bucket:
            length = len(pattern)
            if length < min_match:
                continue
            if data[pos : pos + length] == pattern:
                return (pid, length)
        return None

    def serialize(self):
        """Bytes: count u32, then (len u16, bytes) per pattern."""
        parts = bytearray()
        parts += len(self.patterns).to_bytes(4, "big")
        for pattern in self.patterns:
            parts += len(pattern).to_bytes(2, "big")
            parts += pattern
        return bytes(parts)

    @classmethod
    def deserialize(cls, blob):
        count = int.from_bytes(blob[:4], "big")
        pos = 4
        patterns = []
        for _ in range(count):
            length = int.from_bytes(blob[pos : pos + 2], "big")
            pos += 2
            patterns.append(blob[pos : pos + length])
            pos += length
        return cls(patterns)
