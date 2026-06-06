"""Verify the Rust port of the entropy coder is byte-identical to the Python/C reference.

Skipped unless the cdylib is built (``cd rust && cargo build --release``), so the suite
stays green on machines without a Rust toolchain.
"""
import ctypes
import glob
import os

import numpy as np
import pytest

from compressor import ctxcoder

_HERE = os.path.dirname(__file__)
_SO = glob.glob(os.path.join(_HERE, "..", "rust", "target", "release", "**",
                             "libcompressor_rs.so"), recursive=True)

pytestmark = pytest.mark.skipif(not _SO, reason="Rust cdylib not built (cargo build --release in rust/)")


def _lib():
    lib = ctypes.CDLL(_SO[0])
    lib.ctx_encode.restype = ctypes.c_long
    lib.ctx_decode.restype = None
    return lib


def _rust_encode(lib, res):
    a = np.ascontiguousarray(res, np.int64)
    n = len(a)
    cap = n * 16 + 1024
    out = (ctypes.c_uint8 * cap)()
    m = lib.ctx_encode(a.ctypes.data_as(ctypes.POINTER(ctypes.c_int64)), n, out, cap)
    assert m >= 0
    return bytes(out[:m])


def _rust_decode(lib, blob, n):
    out = (ctypes.c_int64 * n)()
    buf = (ctypes.c_uint8 * len(blob)).from_buffer_copy(blob)
    lib.ctx_decode(buf, len(blob), n, out)
    return np.ctypeslib.as_array(out).copy()


def test_rust_byte_identical_and_cross_compatible():
    lib = _lib()
    rng = np.random.default_rng(0)
    streams = [
        np.zeros(500, np.int64),
        np.array([0, 1, -1, 7, -7, 123456, -9] * 1000, np.int64),
        rng.integers(-1000, 1000, 8000).astype(np.int64),
        np.cumsum(rng.integers(-3, 4, 20000)).astype(np.int64),
    ]
    for s in streams:
        rb = _rust_encode(lib, s)
        pb = ctxcoder.encode(s)
        assert rb == pb                                            # byte-identical to Python/C
        assert np.array_equal(np.asarray(ctxcoder.decode(rb, len(s)), np.int64), s)  # rust->py
        assert np.array_equal(_rust_decode(lib, pb, len(s)), s)    # py->rust
