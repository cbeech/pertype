"""Pattern dictionary: training (mining) and longest-match lookup.

The dictionary is an ordered list of byte patterns. A pattern's index is its
id; the tokenizer references it by that id. ``mine_patterns`` learns a dictionary
from a corpus by counting substring frequencies and greedily keeping the ones
that save the most bytes.
"""
from collections import Counter

# Approximate cost (in bytes) of referencing a pattern in the entropy-coded
# stream. Used only to score candidates during mining; a pattern is worth
# keeping when its length comfortably exceeds this.
_REFERENCE_COST = 2

_DEFAULT_LENGTHS = (3, 4, 5, 6, 8, 10, 12, 16, 20, 24, 32, 48, 64)


def mine_patterns(
    samples,
    max_patterns=4096,
    min_len=3,
    max_len=64,
    max_mining_bytes=1_000_000,
):
    """Learn a :class:`Dictionary` from an iterable of byte samples."""
    blob = b"".join(samples)
    if len(blob) > max_mining_bytes:
        blob = blob[:max_mining_bytes]

    lengths = [l for l in _DEFAULT_LENGTHS if min_len <= l <= max_len]
    if not lengths:
        lengths = [min_len]

    counts = Counter()
    n = len(blob)
    for length in lengths:
        for i in range(0, n - length + 1):
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
