"""Integer arithmetic coder (Witten–Neal–Cleary, 32-bit).

Unlike Huffman, which must spend a whole number of bits per symbol, an
arithmetic coder spends ``-log2(p)`` bits — a fraction of a bit — so it tracks
the true entropy of a skewed distribution far more closely. Symbols are supplied
as ``(cumulative_freq, freq, total)`` triples; the caller's frequency model owns
those numbers.

The classic E1/E2/E3 renormalization with "pending bits" handles carry and
underflow. ``total`` must stay below ``QUARTER`` (it is ~2^16 here), which keeps
the range from collapsing.
"""
from pertype.bitio import BitWriter, BitReader

CODE_BITS = 32
MAX = (1 << CODE_BITS) - 1
HALF = 1 << (CODE_BITS - 1)
QUARTER = 1 << (CODE_BITS - 2)
THREE_QUARTER = 3 * QUARTER


class ArithmeticEncoder:
    def __init__(self, writer=None):
        self.w = writer or BitWriter()
        self.low = 0
        self.high = MAX
        self._pending = 0

    def _emit(self, bit):
        self.w.write_bits(bit, 1)
        while self._pending:
            self.w.write_bits(bit ^ 1, 1)
            self._pending -= 1

    def encode(self, cum, freq, total):
        span = self.high - self.low + 1
        self.high = self.low + span * (cum + freq) // total - 1
        self.low = self.low + span * cum // total
        while True:
            if self.high < HALF:
                self._emit(0)
            elif self.low >= HALF:
                self._emit(1)
                self.low -= HALF
                self.high -= HALF
            elif self.low >= QUARTER and self.high < THREE_QUARTER:
                self._pending += 1
                self.low -= QUARTER
                self.high -= QUARTER
            else:
                break
            self.low <<= 1
            self.high = (self.high << 1) | 1

    def encode_bits(self, value, nbits):
        """Encode ``nbits`` raw bits (uniform), most-significant first."""
        for shift in range(nbits - 1, -1, -1):
            self.encode((value >> shift) & 1, 1, 2)

    def finish(self):
        self._pending += 1
        self._emit(0 if self.low < QUARTER else 1)

    def getvalue(self):
        return self.w.getvalue()


class ArithmeticDecoder:
    def __init__(self, reader):
        self.r = reader if isinstance(reader, BitReader) else BitReader(reader)
        self.low = 0
        self.high = MAX
        self.code = 0
        for _ in range(CODE_BITS):
            self.code = (self.code << 1) | self._bit()

    def _bit(self):
        try:
            return self.r.read_bits(1)
        except EOFError:
            return 0  # arithmetic streams read zero-padding past the end

    def decode_target(self, total):
        span = self.high - self.low + 1
        return ((self.code - self.low + 1) * total - 1) // span

    def update(self, cum, freq, total):
        span = self.high - self.low + 1
        self.high = self.low + span * (cum + freq) // total - 1
        self.low = self.low + span * cum // total
        while True:
            if self.high < HALF:
                pass
            elif self.low >= HALF:
                self.low -= HALF
                self.high -= HALF
                self.code -= HALF
            elif self.low >= QUARTER and self.high < THREE_QUARTER:
                self.low -= QUARTER
                self.high -= QUARTER
                self.code -= QUARTER
            else:
                break
            self.low <<= 1
            self.high = (self.high << 1) | 1
            self.code = (self.code << 1) | self._bit()

    def decode_bits(self, nbits):
        value = 0
        for _ in range(nbits):
            bit = 1 if self.decode_target(2) >= 1 else 0
            self.update(bit, 1, 2)
            value = (value << 1) | bit
        return value
