"""Tests for bit-level I/O."""
from compressor.bitio import BitWriter, BitReader


def test_single_bits_roundtrip():
    bits = [1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 1]
    w = BitWriter()
    for b in bits:
        w.write_bits(b, 1)
    data = w.getvalue()
    r = BitReader(data)
    out = [r.read_bits(1) for _ in bits]
    assert out == bits, out


def test_varied_width_roundtrip():
    values = [(5, 3), (0, 1), (255, 8), (1023, 10), (1, 1), (42, 6), (65535, 16)]
    w = BitWriter()
    for v, n in values:
        w.write_bits(v, n)
    r = BitReader(w.getvalue())
    out = [r.read_bits(n) for _, n in values]
    assert out == [v for v, _ in values], out


def test_byte_alignment_on_flush():
    # 3 bits written -> still produces whole bytes; remainder is zero-padded.
    w = BitWriter()
    w.write_bits(0b101, 3)
    data = w.getvalue()
    assert len(data) == 1
    assert data[0] == 0b101_00000  # MSB-first, zero padded


def test_zero_width_is_noop():
    w = BitWriter()
    w.write_bits(0, 0)
    w.write_bits(0b11, 2)
    r = BitReader(w.getvalue())
    assert r.read_bits(2) == 0b11


def test_empty():
    w = BitWriter()
    assert w.getvalue() == b""


def test_large_value_many_bits():
    v, n = (1 << 31) | 12345, 32
    w = BitWriter()
    w.write_bits(v, n)
    r = BitReader(w.getvalue())
    assert r.read_bits(n) == v
