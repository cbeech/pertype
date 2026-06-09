"""Tests for reversible byte-stream transforms."""
import os

from pertype import transform as T


def test_all_specs_roundtrip():
    datas = [
        b"",
        b"a",
        b"hello world",
        bytes(range(256)) * 3,
        os.urandom(1000),
        b"\x00\x01\x02\x03" * 250,
    ]
    for spec in T.TRANSFORM_SPECS:
        for data in datas:
            assert T.invert(T.apply(data, spec), spec) == data, (spec, len(data))


def test_delta_reduces_smooth_data():
    # A smooth ramp becomes mostly constant after delta-1.
    data = bytes(i % 256 for i in range(2000))
    out = T.apply(data, (("delta", 1),))
    assert out.count(1) > 1900  # nearly all residuals are 1


def test_split_is_a_permutation():
    data = os.urandom(500)
    out = T.apply(data, (("split", 2),))
    assert sorted(out) == sorted(data)
    assert T.invert(out, (("split", 2),)) == data


def test_select_picks_delta_for_strided_numeric():
    # 16-bit little-endian smooth ramp: identity is bad, a delta should win.
    vals = [(i * 3) & 0xFFFF for i in range(4000)]
    data = b"".join(v.to_bytes(2, "little") for v in vals)
    spec = T.select([data])
    assert spec != (), spec  # something better than identity was chosen


def test_select_picks_none_for_text():
    text = (b"the quick brown fox jumps over the lazy dog. " * 200)
    assert T.select([text]) == ()


def test_serialize_roundtrip():
    for spec in T.TRANSFORM_SPECS:
        assert T.deserialize(T.serialize(spec)) == spec
