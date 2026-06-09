"""Tests for canonical, length-limited Huffman coding."""
from pertype.bitio import BitWriter, BitReader
from pertype.huffman import HuffmanCode, build_code_lengths


def _roundtrip(freqs, message):
    h = HuffmanCode.from_frequencies(freqs)
    w = BitWriter()
    h.encode(message, w)
    r = BitReader(w.getvalue())
    out = h.decode(r, len(message))
    assert out == message, (out, message)


def test_basic_roundtrip():
    freqs = {ord("a"): 50, ord("b"): 20, ord("c"): 10, ord("d"): 5}
    msg = [ord(c) for c in "aaabbcddaaa"]
    _roundtrip(freqs, msg)


def test_single_symbol():
    freqs = {7: 100}
    msg = [7, 7, 7, 7]
    h = HuffmanCode.from_frequencies(freqs)
    assert h.code_lengths[7] == 1  # one bit even for a lone symbol
    _roundtrip(freqs, msg)


def test_common_symbol_gets_shorter_code():
    freqs = {1: 1000, 2: 1, 3: 1, 4: 1}
    h = HuffmanCode.from_frequencies(freqs)
    assert h.code_lengths[1] <= h.code_lengths[2]


def test_length_limit_enforced_on_skewed_distribution():
    # Fibonacci weights force long codes in plain Huffman; the limit must cap them.
    limit = 15
    freqs = {}
    a, b = 1, 1
    for sym in range(40):
        freqs[sym] = a
        a, b = b, a + b
    lengths = build_code_lengths(freqs, limit=limit)
    assert max(lengths.values()) <= limit
    # Must still be a valid prefix code: Kraft sum == 1.
    kraft = sum(2 ** (-L) for L in lengths.values())
    assert abs(kraft - 1.0) < 1e-9, kraft
    msg = list(range(40)) + [0, 0, 1, 39]
    _roundtrip(freqs, msg)


def test_serialize_roundtrip():
    freqs = {ord(c): i + 1 for i, c in enumerate("abcdefg")}
    h = HuffmanCode.from_frequencies(freqs)
    blob = h.serialize()
    h2 = HuffmanCode.deserialize(blob)
    assert h2.code_lengths == h.code_lengths


def test_empty_message():
    freqs = {1: 5, 2: 3}
    h = HuffmanCode.from_frequencies(freqs)
    w = BitWriter()
    h.encode([], w)
    r = BitReader(w.getvalue())
    assert h.decode(r, 0) == []
