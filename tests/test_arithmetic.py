"""Tests for the arithmetic coder and frequency model."""
import os

from pertype.arithmetic import ArithmeticEncoder, ArithmeticDecoder
from pertype.bitio import BitReader
from pertype.freqmodel import FrequencyModel


def _roundtrip_symbols(counts, message):
    fm = FrequencyModel.from_counts(counts)
    enc = ArithmeticEncoder()
    for s in message:
        fm.encode(enc, s)
    enc.finish()
    dec = ArithmeticDecoder(BitReader(enc.getvalue()))
    out = [fm.decode(dec) for _ in message]
    assert out == message, (out, message)


def test_uniform_roundtrip():
    counts = {i: 1 for i in range(16)}
    _roundtrip_symbols(counts, [3, 0, 15, 8, 8, 1, 2, 14, 7] * 5)


def test_skewed_roundtrip():
    counts = {0: 1000, 1: 50, 2: 5, 3: 1}
    msg = [0] * 200 + [1] * 10 + [2, 3, 0, 0, 1, 0]
    _roundtrip_symbols(counts, msg)


def test_single_symbol_alphabet():
    counts = {42: 10}
    _roundtrip_symbols(counts, [42] * 20)


def test_empty_message():
    counts = {1: 3, 2: 1}
    _roundtrip_symbols(counts, [])


def test_raw_bits_roundtrip():
    enc = ArithmeticEncoder()
    values = [(5, 3), (0, 1), (255, 8), (1023, 10), (1, 1)]
    for v, n in values:
        enc.encode_bits(v, n)
    enc.finish()
    dec = ArithmeticDecoder(BitReader(enc.getvalue()))
    out = [dec.decode_bits(n) for _, n in values]
    assert out == [v for v, _ in values]


def test_mixed_symbols_and_bits():
    counts = {i: (i + 1) for i in range(10)}
    fm = FrequencyModel.from_counts(counts)
    enc = ArithmeticEncoder()
    plan = [("s", 3), ("b", (5, 4)), ("s", 9), ("s", 0), ("b", (170, 8)), ("s", 7)]
    for kind, val in plan:
        if kind == "s":
            fm.encode(enc, val)
        else:
            enc.encode_bits(val[0], val[1])
    enc.finish()
    dec = ArithmeticDecoder(BitReader(enc.getvalue()))
    out = []
    for kind, val in plan:
        if kind == "s":
            out.append(("s", fm.decode(dec)))
        else:
            out.append(("b", (dec.decode_bits(val[1]), val[1])))
    assert out == [(k, v) for k, v in plan]


def test_serialize_roundtrip_preserves_coding():
    counts = {i: (i * i + 1) for i in range(30)}
    fm = FrequencyModel.from_counts(counts)
    fm2 = FrequencyModel.deserialize(fm.serialize())
    assert fm2.symbols == fm.symbols and fm2.freqs == fm.freqs and fm2.total == fm.total
    # A stream coded by fm must decode under fm2.
    msg = [os.urandom(1)[0] % 30 for _ in range(100)]
    enc = ArithmeticEncoder()
    for s in msg:
        fm.encode(enc, s)
    enc.finish()
    dec = ArithmeticDecoder(BitReader(enc.getvalue()))
    assert [fm2.decode(dec) for _ in msg] == msg
