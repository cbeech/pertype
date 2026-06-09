"""Round-trip and selection tests for the columnar record codec."""
import numpy as np

from pertype import columnar as C


def _records(cols, schema):
    """Interleave int columns into a record byte stream for the given field widths."""
    n = len(cols[0])
    mat = np.empty((n, sum(schema)), np.uint8)
    off = 0
    for c, w in zip(cols, schema):
        v = np.asarray(c, np.int64)
        for b in range(w):
            mat[:, off + b] = ((v >> (8 * b)) & 0xFF).astype(np.uint8)
        off += w
    return mat.tobytes()


def test_roundtrip_schema_int32_columns():
    rng = np.random.default_rng(0)
    n = 4000
    X = np.cumsum(rng.integers(-3, 4, n))            # smooth -> delta wins
    Y = np.cumsum(rng.integers(-2, 3, n))
    I = rng.integers(0, 500, n)                       # noisy u16 -> raw wins
    data = _records([X & 0xFFFFFFFF, Y & 0xFFFFFFFF, I], [4, 4, 2])
    blob = C.encode(data, schema=[4, 4, 2])
    assert C.decode(blob) == data
    assert len(blob) < len(data)                      # smooth columns compress


def test_roundtrip_auto_width_uniform():
    rng = np.random.default_rng(1)
    n = 3000
    A = np.cumsum(rng.integers(-1, 2, n)) & 0xFFFFFFFF
    B = np.cumsum(rng.integers(-1, 2, n)) & 0xFFFFFFFF
    data = _records([A, B], [4, 4])                   # width 8, divisible by 4/2/1
    blob = C.encode(data, width=8)                    # searches uniform tilings
    assert C.decode(blob) == data
    assert len(blob) < len(data)


def test_trailing_remainder_preserved():
    data = _records([np.arange(100) & 0xFFFFFFFF], [4]) + b"\x07\x08\x09"
    blob = C.encode(data, schema=[4])
    assert C.decode(blob) == data                     # 3 trailing bytes survive exactly


def test_store_fallback_never_expands():
    rng = np.random.default_rng(2)
    rnd = rng.integers(0, 256, 50000, dtype=np.uint8).tobytes()
    blob = C.encode(rnd)
    assert C.decode(blob) == rnd
    assert len(blob) <= len(rnd) + 5                  # high-entropy -> store, +5B header


def test_empty_and_tiny():
    for d in (b"", b"a", b"abc", b"\x00" * 9):
        assert C.decode(C.encode(d)) == d


def test_detect_width_finds_period_and_rejects_noise():
    data = _records([np.arange(2000) & 0xFFFFFFFF, np.zeros(2000, int)], [4, 4])
    assert C.detect_width(data) == 8
    rnd = np.random.default_rng(3).integers(0, 256, 40000, dtype=np.uint8).tobytes()
    assert C.detect_width(rnd) == 0                   # no periodicity -> 0 (store)
