"""Bit-level I/O, MSB-first within each byte.

``BitWriter`` accumulates bits and pads the final byte with zeros on output.
``BitReader`` reads them back in the same order. MSB-first is the convention
used for the Huffman codes elsewhere in this package, so both share it.
"""


class BitWriter:
    def __init__(self):
        self._bytes = bytearray()
        self._cur = 0          # bits accumulated but not yet flushed to a byte
        self._nbits = 0        # number of valid bits in ``_cur`` (0..7)

    def write_bits(self, value, n):
        """Append the low ``n`` bits of ``value``, most-significant first."""
        if n == 0:
            return
        if value >> n:
            raise ValueError(f"value {value} does not fit in {n} bits")
        for shift in range(n - 1, -1, -1):
            bit = (value >> shift) & 1
            self._cur = (self._cur << 1) | bit
            self._nbits += 1
            if self._nbits == 8:
                self._bytes.append(self._cur)
                self._cur = 0
                self._nbits = 0

    def getvalue(self):
        """Return all written bits as bytes; a partial final byte is zero-padded."""
        if self._nbits == 0:
            return bytes(self._bytes)
        last = self._cur << (8 - self._nbits)
        return bytes(self._bytes) + bytes([last])


class BitReader:
    def __init__(self, data):
        self._data = data
        self._pos = 0          # absolute bit position from the start

    def read_bits(self, n):
        """Read ``n`` bits (most-significant first) and return them as an int."""
        if n == 0:
            return 0
        value = 0
        for _ in range(n):
            byte_index = self._pos >> 3
            bit_index = 7 - (self._pos & 7)
            if byte_index >= len(self._data):
                raise EOFError("read past end of bit stream")
            bit = (self._data[byte_index] >> bit_index) & 1
            value = (value << 1) | bit
            self._pos += 1
        return value
