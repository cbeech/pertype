"""Verify the Rust port is byte-identical to the Python/C reference and cross-compatible.

Covers all three ported codecs (ctxcoder, CALIC, columnar). Skipped unless the cdylib is
built (``cd rust && cargo build --release``), so the suite stays green without a Rust
toolchain.
"""
import ctypes
import glob
import os

import numpy as np
import pytest

from compressor import auto, columnar, csvcolumnar, ctxcoder, floatcodec, predictors, transform

_HERE = os.path.dirname(__file__)
_SO = glob.glob(os.path.join(_HERE, "..", "rust", "target", "release", "**",
                             "libcompressor_rs.so"), recursive=True)

pytestmark = pytest.mark.skipif(not _SO, reason="Rust cdylib not built (cargo build --release in rust/)")


@pytest.fixture(scope="module")
def lib():
    lb = ctypes.CDLL(_SO[0])
    for name in ("ctx_encode", "calic_codec_encode", "columnar_encode", "columnar_decode",
                 "float_encode", "float_decode", "csv_encode", "csv_decode",
                 "auto_encode", "auto_decode"):
        getattr(lb, name).restype = ctypes.c_long
    return lb


def _ctx_encode(lib, res):
    a = np.ascontiguousarray(res, np.int64)
    out = (ctypes.c_uint8 * (len(a) * 16 + 1024))()
    m = lib.ctx_encode(a.ctypes.data_as(ctypes.POINTER(ctypes.c_int64)), len(a), out, len(out))
    assert m >= 0
    return bytes(out[:m])


def _calic_encode(lib, img, scale):
    a = np.ascontiguousarray(img, np.int64)
    h, w = a.shape
    out = (ctypes.c_uint8 * (h * w * 8 + 1024))()
    m = lib.calic_codec_encode(a.ctypes.data_as(ctypes.POINTER(ctypes.c_int64)), h, w, scale, out, len(out))
    assert m >= 0
    return bytes(out[:m])


def _col(lib, fn, data, *extra):
    out = (ctypes.c_uint8 * (len(data) * 256 + (1 << 20)))()   # generous: decode can expand a lot
    buf = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
    m = fn(buf, len(data), *extra, out, len(out))
    assert m >= 0
    return bytes(out[:m])


def test_ctxcoder_byte_identical(lib):
    rng = np.random.default_rng(0)
    for s in (np.zeros(500, np.int64),
              np.array([0, 1, -1, 7, -7, 123456, -9] * 800, np.int64),
              np.cumsum(rng.integers(-3, 4, 20000)).astype(np.int64)):
        rb = _ctx_encode(lib, s)
        assert rb == ctxcoder.encode(s)                                   # byte-identical
        assert np.array_equal(np.asarray(ctxcoder.decode(rb, len(s)), np.int64), s)


def test_calic_byte_identical(lib):
    rng = np.random.default_rng(1)
    img = (np.cumsum(rng.integers(-3, 4, (96, 112)), axis=1) % 256).astype(np.int32)
    for scale in (1, 4):
        rb = _calic_encode(lib, img, scale)
        assert rb == predictors.calic_full_encode(np.ascontiguousarray(img), scale)
        back = predictors.calic_full_decode(rb, img.shape[0], img.shape[1], scale)
        assert np.array_equal(back.astype(np.int32), img)


def test_columnar_byte_identical(lib):
    rng = np.random.default_rng(2)
    n = 3000
    cols = [np.cumsum(rng.integers(-3, 4, n)).astype("<i4") for _ in range(3)]
    rec = np.empty((n, 12), np.uint8)
    for j in range(3):
        rec[:, j * 4:j * 4 + 4] = cols[j].view(np.uint8).reshape(n, 4)
    data = rec.tobytes()
    rb = _col(lib, lib.columnar_encode, data, 12)                         # width 12
    assert rb == columnar.encode(data, width=12)                          # byte-identical
    assert columnar.decode(rb) == data                                    # rust -> py
    pb = columnar.encode(data, width=12)
    assert _col(lib, lib.columnar_decode, pb) == data                     # py -> rust


def test_floatcodec_cross_compatible(lib):
    # low-cardinality f32 grid: same ratio + cross-decodable (deflate dict not byte-identical)
    rng = np.random.default_rng(3)
    grid = (np.cumsum(rng.integers(-3, 4, 20000)) / 100.0).astype("<f4")
    data = np.ascontiguousarray(grid).tobytes()
    rb = _col(lib, lib.float_encode, data, 4)
    assert _col(lib, lib.float_decode, rb) == data                        # rust round-trip
    assert floatcodec.decode(rb) == data                                  # py decodes rust
    assert _col(lib, lib.float_decode, floatcodec.encode(data, 4)) == data  # rust decodes py


def test_csvcolumnar_cross_compatible(lib):
    rows = ["t;v;n"]
    v = 1000
    for i in range(2000):
        v += (i * 7 % 11) - 5
        rows.append(f"2024-01-01;{v/100:.2f};{i}")
    data = ("\n".join(rows) + "\n").encode()
    rb = _col(lib, lib.csv_encode, data)
    assert _col(lib, lib.csv_decode, rb) == data                          # rust round-trip
    assert csvcolumnar.decode(rb) == data                                 # py decodes rust
    assert _col(lib, lib.csv_decode, csvcolumnar.encode(data)) == data    # rust decodes py


def test_transform_byte_identical(lib):
    data = bytes((i * 37 >> 3) & 0xFF for i in range(1000))

    def rust_op(fn, arg):
        out = (ctypes.c_uint8 * len(data))()
        buf = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
        fn(buf, len(data), arg, out)
        return bytes(out)

    for s in (1, 2, 4):
        assert rust_op(lib.transform_delta_fwd, s) == transform.apply(data, (("delta", s),))
    for n in (2, 3, 8):
        assert rust_op(lib.transform_split_fwd, n) == transform.apply(data, (("split", n),))


def test_auto_cross_compatible(lib):
    # the Rust auto produces the same AZ container Python's auto_decompress reads
    rows = ["a;b;n"] + [f"2024-01-01;{i/100:.2f};{i}" for i in range(1500)]
    data = ("\n".join(rows) + "\n").encode()
    rb = _col(lib, lib.auto_encode, data)
    assert auto.auto_decompress(rb) == data                               # py decodes rust .az
    assert _col(lib, lib.auto_decode, auto.auto_compress(data)) == data   # rust decodes py .az
