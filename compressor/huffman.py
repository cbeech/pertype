"""Canonical, length-limited Huffman coding.

Code lengths are produced by the **package-merge** algorithm (Larmore-Hirschberg),
which yields optimal codes subject to a maximum length. Capping the length keeps
the serialized table compact and avoids pathological deep trees. Codes are then
assigned canonically (RFC 1951 style) so the decoder can rebuild them from the
lengths alone.
"""
from collections import Counter

MAX_CODE_LENGTH = 15


def build_code_lengths(freqs, limit=MAX_CODE_LENGTH):
    """Map ``{symbol: frequency}`` -> ``{symbol: code_length}`` with length <= limit.

    Only symbols with non-zero frequency receive a length.
    """
    items = sorted((w, s) for s, w in freqs.items() if w > 0)
    n = len(items)
    if n == 0:
        return {}
    if n == 1:
        return {items[0][1]: 1}
    if (1 << limit) < n:
        raise ValueError(f"limit={limit} too small for {n} symbols")

    # Each node carries (weight, [symbols it covers]). Leaves are reused as the
    # "coins" at every denomination level; packages merge the previous level.
    leaves = [(w, (s,)) for w, s in items]
    prev = []
    for _ in range(limit):
        packaged = [
            (prev[i][0] + prev[i + 1][0], prev[i][1] + prev[i + 1][1])
            for i in range(0, len(prev) - 1, 2)
        ]
        merged = leaves + packaged
        merged.sort(key=lambda x: x[0])
        prev = merged

    chosen = prev[: 2 * n - 2]
    counts = Counter()
    for _, syms in chosen:
        for s in syms:
            counts[s] += 1
    return dict(counts)


def canonical_codes(code_lengths):
    """Assign canonical codes. Returns ``{symbol: (code_int, length)}``."""
    if not code_lengths:
        return {}
    max_len = max(code_lengths.values())
    bl_count = [0] * (max_len + 1)
    for length in code_lengths.values():
        bl_count[length] += 1

    next_code = [0] * (max_len + 1)
    code = 0
    for bits in range(1, max_len + 1):
        code = (code + bl_count[bits - 1]) << 1
        next_code[bits] = code

    codes = {}
    for symbol in sorted(code_lengths):
        length = code_lengths[symbol]
        codes[symbol] = (next_code[length], length)
        next_code[length] += 1
    return codes


class HuffmanCode:
    def __init__(self, code_lengths):
        self.code_lengths = dict(code_lengths)
        self._codes = canonical_codes(self.code_lengths)
        # Decode table keyed by (length, code) -> symbol.
        self._decode = {(length, code): s for s, (code, length) in self._codes.items()}
        self._min_len = min((l for _, l in self._codes.values()), default=0)
        self._max_len = max((l for _, l in self._codes.values()), default=0)

    @classmethod
    def from_frequencies(cls, freqs, limit=MAX_CODE_LENGTH):
        return cls(build_code_lengths(freqs, limit=limit))

    def encode(self, symbols, writer):
        for s in symbols:
            code, length = self._codes[s]
            writer.write_bits(code, length)

    def decode_symbol(self, reader):
        code = 0
        length = 0
        while True:
            code = (code << 1) | reader.read_bits(1)
            length += 1
            sym = self._decode.get((length, code))
            if sym is not None:
                return sym
            if length > self._max_len:
                raise ValueError("invalid Huffman stream")

    def decode(self, reader, count):
        return [self.decode_symbol(reader) for _ in range(count)]

    def serialize(self):
        """Compact bytes: count, then (symbol u32, length u8) per entry."""
        parts = bytearray()
        parts += len(self.code_lengths).to_bytes(4, "big")
        for symbol in sorted(self.code_lengths):
            parts += symbol.to_bytes(4, "big")
            parts += bytes([self.code_lengths[symbol]])
        return bytes(parts)

    @classmethod
    def deserialize(cls, blob):
        n = int.from_bytes(blob[:4], "big")
        pos = 4
        lengths = {}
        for _ in range(n):
            symbol = int.from_bytes(blob[pos : pos + 4], "big")
            length = blob[pos + 4]
            lengths[symbol] = length
            pos += 5
        return cls(lengths)
