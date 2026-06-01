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


def _roundtrip_prefix(data, d, prefix):
    tokens = tokenize(data, d, use_lz=True, prefix=prefix)
    assert detokenize(tokens, d, prefix=prefix) == data, (data, tokens)


def test_prefix_blob_roundtrips():
    d = Dictionary([])
    prefix = b"<html><head><title>Report</title></head><body><table>"
    # data shares structure with the blob, so it should match back into it.
    data = b"<html><head><title>Page 2</title></head><body><table><tr>"
    _roundtrip_prefix(data, d, prefix)


def test_prefix_blob_match_actually_used():
    d = Dictionary([])
    prefix = b"ABCDEFGHIJKLMNOP" * 4
    data = b"zz" + b"ABCDEFGHIJKLMNOP" + b"qq"  # only matchable via the blob
    tokens = tokenize(data, d, use_lz=True, prefix=prefix)
    assert any(t[0] == "match" for t in tokens)
    _roundtrip_prefix(data, d, prefix)


def test_empty_prefix_equals_no_prefix():
    d = Dictionary([])
    data = b"some repeated data data data"
    assert tokenize(data, d, use_lz=True, prefix=b"") == tokenize(data, d, use_lz=True)
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
