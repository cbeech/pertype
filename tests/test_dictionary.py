"""Tests for the pattern miner and longest-match matcher."""
from compressor.dictionary import Dictionary, mine_patterns


def test_mines_frequent_substring():
    samples = [b'{"name":"alice"}', b'{"name":"bob"}', b'{"name":"carol"}']
    d = mine_patterns(samples, max_patterns=32, min_len=3, max_len=16)
    assert any(b'"name":"' in p for p in d.patterns), d.patterns


def test_longest_match_prefers_longer_pattern():
    d = Dictionary([b"ab", b"abcd"])
    sym, length = d.match(b"abcde", 0, min_match=2)
    assert length == 4
    assert d.patterns[sym] == b"abcd"


def test_no_match_returns_none():
    d = Dictionary([b"xyz"])
    assert d.match(b"abc", 0, min_match=2) is None


def test_match_respects_min_match():
    d = Dictionary([b"ab"])
    assert d.match(b"abc", 0, min_match=3) is None


def test_serialize_roundtrip():
    d = Dictionary([b"hello", b"world", b"\x00\x01\x02"])
    d2 = Dictionary.deserialize(d.serialize())
    assert d2.patterns == d.patterns


def test_match_at_offset():
    d = Dictionary([b"def"])
    res = d.match(b"abcdef", 3, min_match=3)
    assert res is not None and res[1] == 3
