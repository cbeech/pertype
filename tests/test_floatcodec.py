"""Round-trip and behaviour tests for the low-cardinality float codec."""
import numpy as np

from pertype import floatcodec as F


def test_low_cardinality_float32_roundtrip_and_wins():
    rng = np.random.default_rng(0)
    # a smooth fixed-precision grid: few distinct values, spatially correlated
    base = np.cumsum(rng.integers(-3, 4, (200, 300)), axis=1).astype(np.float32) / 100.0
    data = np.ascontiguousarray(base).tobytes()
    blob = F.encode(data, 4)
    assert F.decode(blob) == data
    assert len(blob) < len(data) // 3            # dictionary + delta crushes it


def test_float64_roundtrip():
    vals = (np.arange(20000) % 500).astype(np.float64) / 8.0
    data = vals.tobytes()
    blob = F.encode(data, 8)
    assert F.decode(blob) == data
    assert len(blob) < len(data) // 2


def test_special_values_bit_exact():
    vals = np.array([0.0, -0.0, np.nan, np.inf, -np.inf, 1.5, 1.5] * 2000, np.float64)
    data = vals.tobytes()
    assert F.decode(F.encode(data, 8)) == data   # -0.0 and NaN survive by bit pattern


def test_high_cardinality_falls_back_to_store():
    rnd = np.random.default_rng(1).random(100000).astype(np.float32).tobytes()
    blob = F.encode(rnd, 4)
    assert F.decode(blob) == rnd
    assert blob[4] == F.M_STORE and len(blob) <= len(rnd) + 8


def test_trailing_bytes_and_tiny_and_bad_itemsize():
    vals = (np.arange(1000) % 50).astype(np.float32)
    data = vals.tobytes() + b"\x09\x08\x07"
    assert F.decode(F.encode(data, 4)) == data
    for d in (b"", b"abc", vals[:4].tobytes()):
        assert F.decode(F.encode(d, 4)) == d
    assert F.decode(F.encode(b"xxxxxx", 3)) == b"xxxxxx"   # unsupported itemsize -> store
