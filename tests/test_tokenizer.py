"""Tests for reversible tokenization (literals, dict refs, LZ matches)."""
import os

from compressor.dictionary import Dictionary, mine_patterns
from compressor.tokenizer import tokenize, detokenize


def _roundtrip(data, d):
    tokens = tokenize(data, d)
    assert detokenize(tokens, d) == data, (data, tokens)


def test_literals_only_when_nothing_matches():
    d = Dictionary([])
    data = b"hello world"  # no 3-byte repeat, no dictionary
    tokens = tokenize(data, d)
    assert all(t[0] == "lit" for t in tokens)
    _roundtrip(data, d)


def test_uses_dictionary_pattern():
    d = Dictionary([b'"name":"'])
    data = b'{"name":"x"}'
    tokens = tokenize(data, d)
    assert any(t[0] == "dict" for t in tokens)
    _roundtrip(data, d)


def test_uses_in_file_lz_match():
    d = Dictionary([])  # no dictionary, so the repeat must be an LZ match
    data = b"abcdefg_abcdefg_abcdefg"
    tokens = tokenize(data, d)
    assert any(t[0] == "match" for t in tokens)
    _roundtrip(data, d)


def test_overlapping_match_roundtrips():
    # distance 1, long run -> classic overlapping LZ copy
    d = Dictionary([])
    data = b"x" + b"a" * 100
    _roundtrip(data, d)


def test_lazy_match_roundtrips():
    # Crafted so the match at pos is shorter than the match one byte later,
    # exercising the lazy one-byte-lookahead deferral path.
    d = Dictionary([])
    data = b"abcXYZ" + b"_abcdefgh" + b"Qabcdefgh" + b"abcdefgh"
    _roundtrip(data, d)


def test_lazy_does_not_lose_data_on_repetitive_input():
    d = Dictionary([])
    data = (b"the quick brown fox " * 30) + b"!" + (b"the quick brown box " * 30)
    _roundtrip(data, d)


def test_roundtrip_with_bytes_absent_from_dictionary():
    d = Dictionary([b"abc", b"xyz"])
    data = bytes(range(256)) + b"abcxyzabc" + bytes([200, 201])
    _roundtrip(data, d)


def test_roundtrip_empty():
    _roundtrip(b"", Dictionary([b"abc"]))


def test_roundtrip_random_and_samples():
    samples = [b'{"k":%d,"v":"item%d"}' % (i, i) for i in range(50)]
    d = mine_patterns(samples, max_patterns=64)
    for _ in range(20):
        _roundtrip(os.urandom(200), d)
    for s in samples:
        _roundtrip(s, d)
