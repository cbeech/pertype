"""A static frequency model that drives the arithmetic coder.

Raw training counts are quantized to a fixed total so encoder and decoder agree
exactly on every probability. The quantized counts are what gets serialized;
deserialization rebuilds the model from them verbatim (no re-quantization), so a
freshly trained model and a loaded one code identically.
"""
import bisect
import math

TARGET_TOTAL = 1 << 16


class FrequencyModel:
    def __init__(self, symbols, freqs):
        # ``symbols`` sorted ascending; ``freqs`` are final quantized counts.
        self.symbols = symbols
        self.freqs = freqs
        self.index = {s: i for i, s in enumerate(symbols)}
        self.cum = [0] * (len(symbols) + 1)
        for i, f in enumerate(freqs):
            self.cum[i + 1] = self.cum[i] + f
        self.total = self.cum[-1]

    @classmethod
    def from_counts(cls, counts):
        """Build from raw ``{symbol: count}`` (every count must be >= 1)."""
        symbols = sorted(counts)
        raw_total = sum(counts[s] for s in symbols)
        freqs = [max(1, counts[s] * TARGET_TOTAL // raw_total) for s in symbols]
        return cls(symbols, freqs)

    def encode(self, encoder, symbol):
        i = self.index[symbol]
        encoder.encode(self.cum[i], self.freqs[i], self.total)

    def decode(self, decoder):
        target = decoder.decode_target(self.total)
        i = bisect.bisect_right(self.cum, target) - 1
        decoder.update(self.cum[i], self.freqs[i], self.total)
        return self.symbols[i]

    def cost_bits(self, symbol):
        """Bits the arithmetic coder will spend on ``symbol`` (for pricing)."""
        return math.log2(self.total / self.freqs[self.index[symbol]])

    def serialize(self):
        parts = bytearray()
        parts += len(self.symbols).to_bytes(4, "big")
        for s, f in zip(self.symbols, self.freqs):
            parts += s.to_bytes(4, "big")
            parts += f.to_bytes(4, "big")
        return bytes(parts)

    @classmethod
    def deserialize(cls, blob):
        n = int.from_bytes(blob[:4], "big")
        pos = 4
        symbols, freqs = [], []
        for _ in range(n):
            symbols.append(int.from_bytes(blob[pos : pos + 4], "big"))
            freqs.append(int.from_bytes(blob[pos + 4 : pos + 8], "big"))
            pos += 8
        return cls(symbols, freqs)
