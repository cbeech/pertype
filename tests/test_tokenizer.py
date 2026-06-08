"""Tests for reversible tokenization (literals, dict refs, LZ matches)."""
import os

import os

from compressor.dictionary import Dictionary, mine_patterns
from compressor.tokenizer import (
    MAX_CHAIN, MIN_MATCH, _match_len, adaptive_max_chain, tokenize, tokenize_optimal,
    detokenize, value_slot,
)


def test_adaptive_max_chain_taper():
    # deep on small inputs, tapering to the fixed floor on large ones; never below MAX_CHAIN
    assert adaptive_max_chain(0) == 2048           # empty / tiny -> deepest
    assert adaptive_max_chain(512) == 2048         # small file -> deepest
    assert adaptive_max_chain(2048) == 2048
    assert adaptive_max_chain(4096) == 1024        # 2048^2 / 4096
    assert adaptive_max_chain(8192) == 512
    assert adaptive_max_chain(1 << 20) == MAX_CHAIN  # large -> floor (128)
    # monotonically non-increasing in size, always >= the fixed default
    prev = adaptive_max_chain(1)
    for n in (10, 100, 1000, 10_000, 100_000, 1_000_000):
        cur = adaptive_max_chain(n)
        assert MAX_CHAIN <= cur <= prev
        prev = cur


def _naive_match_len(buf, i, j, limit):
    n = 0
    while n < limit and buf[i + n] == buf[j + n]:
        n += 1
    return n


def test_match_len_matches_naive():
    cases = [
        (b"abcabcabcXYZ", 0, 3, 12),
        (b"aaaaaaaa", 0, 1, 7),
        (b"xy" + b"\x00" * 50 + b"q", 2, 3, 49),
        (b"abcdefgh", 0, 4, 4),
        (bytes(range(256)) * 3, 0, 256, 512),
    ]
    for buf, i, j, limit in cases:
        assert _match_len(buf, i, j, limit) == _naive_match_len(buf, i, j, limit)
    # randomized
    blob = os.urandom(300) + b"\x00" * 300
    for _ in range(200):
        i, j = os.urandom(1)[0], os.urandom(1)[0]
        limit = min(len(blob) - i, len(blob) - j, 1 + os.urandom(1)[0])
        assert _match_len(blob, i, j, limit) == _naive_match_len(blob, i, j, limit)


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


# A simple fixed-price cost model for exercising the optimal parser directly.
def _flat_costs(lit=8.0, dictc=6.0):
    def lit_cost(_byte):
        return lit

    def dict_cost(_pid):
        return dictc

    def match_cost(length, distance):
        lslot, _ = value_slot(length - MIN_MATCH + 1)
        dslot, _ = value_slot(distance)
        return 6.0 + lslot + dslot  # one symbol each + extra bits

    return lit_cost, dict_cost, match_cost


def _total_cost(tokens, dictionary, costs):
    lit_cost, dict_cost, match_cost = costs
    total = 0.0
    for tok in tokens:
        if tok[0] == "lit":
            total += lit_cost(tok[1])
        elif tok[0] == "dict":
            total += dict_cost(tok[1])
        else:
            total += match_cost(tok[1], tok[2])
    return total


def test_optimal_roundtrips():
    d = Dictionary([])
    data = b"the quick brown fox " * 20 + b"!" + b"the quick brown fox jumps " * 10
    tokens = tokenize_optimal(data, d, _flat_costs())
    assert detokenize(tokens, d) == data


def test_optimal_roundtrips_with_prefix():
    d = Dictionary([])
    prefix = b"<html><head><title>Report</title></head><body><table><tr><td>"
    data = b"<html><head><title>Page</title></head><body><table><tr><td>x"
    tokens = tokenize_optimal(data, d, _flat_costs(), prefix=prefix)
    assert detokenize(tokens, d, prefix=prefix) == data


def test_optimal_never_costlier_than_lazy():
    d = mine_patterns([b'{"name":"item%d","v":%d}' % (i, i) for i in range(40)])
    costs = _flat_costs()
    for data in (
        b'{"name":"itemXYZ","v":999}' * 8,
        b"abcabcabcabcabcabc def def def ghi",
        bytes(range(256)),
    ):
        opt = _total_cost(tokenize_optimal(data, d, costs), d, costs)
        lazy = _total_cost(tokenize(data, d, use_lz=True), d, costs)
        assert opt <= lazy + 1e-9, (opt, lazy)


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
