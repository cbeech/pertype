"""Native (C, via ctypes) implementations of hot primitives, with auto-build.

The C sources in ``_native/`` are compiled to a shared library with gcc on first
import (recompiled when the source changes). If gcc or the source is unavailable,
``HAVE_NATIVE`` is False and callers fall back to the pure-Python reference. The
native code is required to be bit-identical to that reference.

This is the seam the optimised port grows along: add a C primitive + a thin
wrapper here, keep the Python fallback, and dispatch on ``HAVE_NATIVE``.
"""
import ctypes
import os
import subprocess

import numpy as np

_DIR = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_DIR, "_native", "audio.c")
_SO = os.path.join(_DIR, "_native", "audio.so")

HAVE_NATIVE = False
_lib = None


def _build():
    if not os.path.exists(_SRC):
        return False
    if os.path.exists(_SO) and os.path.getmtime(_SO) >= os.path.getmtime(_SRC):
        return True
    try:
        subprocess.run(
            # -fwrapv: signed overflow wraps like numpy int64.
            # -ffp-contract=off: no FMA, so the float `run` update matches Python.
            ["gcc", "-O3", "-fPIC", "-fwrapv", "-ffp-contract=off", "-shared",
             "-o", _SO, _SRC],
            check=True, capture_output=True,
        )
        return os.path.exists(_SO)
    except Exception:
        return False


_I64 = ctypes.POINTER(ctypes.c_int64)
_I32 = ctypes.POINTER(ctypes.c_int32)
_U8 = ctypes.POINTER(ctypes.c_uint8)
try:
    if _build():
        _lib = ctypes.CDLL(_SO)
        for fn in ("lms_fwd", "lms_inv"):
            getattr(_lib, fn).argtypes = [
                _I64, _I64, ctypes.c_long, ctypes.c_int, ctypes.c_int
            ]
            getattr(_lib, fn).restype = None
        for fn in ("fixed2_fwd", "fixed2_inv"):
            getattr(_lib, fn).argtypes = [_I64, _I64, ctypes.c_long]
            getattr(_lib, fn).restype = None
        _lib.rice_encode.argtypes = [_I64, ctypes.c_long, _U8, ctypes.c_long]
        _lib.rice_encode.restype = ctypes.c_long
        _lib.rice_decode.argtypes = [_U8, ctypes.c_long, _I64]
        _lib.rice_decode.restype = None
        for fn in ("delta_fwd", "delta_inv"):
            getattr(_lib, fn).argtypes = [_U8, _U8, ctypes.c_long, ctypes.c_int]
            getattr(_lib, fn).restype = None
        _lib.ctx_encode.argtypes = [_I64, ctypes.c_long, _U8, ctypes.c_long]
        _lib.ctx_encode.restype = ctypes.c_long
        _lib.ctx_decode.argtypes = [_U8, ctypes.c_long, ctypes.c_long, _I64]
        _lib.ctx_decode.restype = None
        _ci = ctypes.c_int
        _lib.lz_encode.argtypes = [
            _I32, _I64, _I64, ctypes.c_long,
            _I32, _ci, _I32, _ci, _I32, _ci,
            _ci, _ci, _U8, ctypes.c_long,
        ]
        _lib.lz_encode.restype = ctypes.c_long
        _lib.lz_decode.argtypes = [
            _U8, ctypes.c_long, ctypes.c_long,
            _I32, _ci, _I32, _ci, _I32, _ci,
            _ci, _ci, _ci, _I32, _I64, _I64,
        ]
        _lib.lz_decode.restype = None
        HAVE_NATIVE = True
except Exception:
    HAVE_NATIVE = False


def _ptr(a):
    return a.ctypes.data_as(_I64)


def _u8ptr(a):
    return a.ctypes.data_as(_U8)


def _i32ptr(a):
    return a.ctypes.data_as(_I32)


def lms_fwd(x, taps, shift):
    x = np.ascontiguousarray(x, dtype=np.int64)
    out = np.empty(len(x), dtype=np.int64)
    _lib.lms_fwd(_ptr(x), _ptr(out), len(x), taps, shift)
    return out


def lms_inv(e, taps, shift):
    e = np.ascontiguousarray(e, dtype=np.int64)
    out = np.empty(len(e), dtype=np.int64)
    _lib.lms_inv(_ptr(e), _ptr(out), len(e), taps, shift)
    return out


def fixed2_fwd(x):
    x = np.ascontiguousarray(x, dtype=np.int64)
    out = np.empty(len(x), dtype=np.int64)
    _lib.fixed2_fwd(_ptr(x), _ptr(out), len(x))
    return out


def fixed2_inv(e):
    e = np.ascontiguousarray(e, dtype=np.int64)
    out = np.empty(len(e), dtype=np.int64)
    _lib.fixed2_inv(_ptr(e), _ptr(out), len(e))
    return out


def rice_encode(res):
    res = np.ascontiguousarray(res, dtype=np.int64)
    n = len(res)
    cap = n * 8 + 64
    while True:
        out = np.empty(cap, dtype=np.uint8)
        ln = _lib.rice_encode(_ptr(res), n, _u8ptr(out), cap)
        if ln >= 0:
            return out[:ln].tobytes()
        cap *= 2


def rice_decode(blob, n):
    buf = np.frombuffer(blob + b"\x00\x00", dtype=np.uint8)  # slack to avoid over-read
    out = np.empty(n, dtype=np.int64)
    _lib.rice_decode(_u8ptr(np.ascontiguousarray(buf)), n, _ptr(out))
    return out


def delta_fwd(data, stride):
    src = np.frombuffer(data, dtype=np.uint8)
    out = np.empty(len(src), dtype=np.uint8)
    _lib.delta_fwd(_u8ptr(np.ascontiguousarray(src)), _u8ptr(out), len(src), stride)
    return out.tobytes()


def delta_inv(data, stride):
    src = np.frombuffer(data, dtype=np.uint8)
    out = np.empty(len(src), dtype=np.uint8)
    _lib.delta_inv(_u8ptr(np.ascontiguousarray(src)), _u8ptr(out), len(src), stride)
    return out.tobytes()


def ctx_encode(res):
    res = np.ascontiguousarray(res, dtype=np.int64)
    n = len(res)
    cap = n * 8 + 64
    while True:
        out = np.empty(cap, dtype=np.uint8)
        ln = _lib.ctx_encode(_ptr(res), n, _u8ptr(out), cap)
        if ln >= 0:
            return out[:ln].tobytes()
        cap *= 2


def ctx_decode(blob, n):
    buf = np.frombuffer(blob, dtype=np.uint8).copy()  # writable & contiguous
    out = np.empty(n, dtype=np.int64)
    _lib.ctx_decode(_u8ptr(buf), len(blob), n, _ptr(out))
    return out


def lz_encode(kind, aval, bval, mcum, dcum, ocum, len_base, min_match):
    kind = np.ascontiguousarray(kind, dtype=np.int32)
    aval = np.ascontiguousarray(aval, dtype=np.int64)
    bval = np.ascontiguousarray(bval, dtype=np.int64)
    mcum = np.ascontiguousarray(mcum, dtype=np.int32)
    dcum = np.ascontiguousarray(dcum, dtype=np.int32)
    ocum = np.ascontiguousarray(ocum, dtype=np.int32)
    n = len(kind)
    cap = n * 8 + 1024
    while True:
        out = np.empty(cap, dtype=np.uint8)
        ln = _lib.lz_encode(
            _i32ptr(kind), _ptr(aval), _ptr(bval), n,
            _i32ptr(mcum), len(mcum) - 1, _i32ptr(dcum), len(dcum) - 1,
            _i32ptr(ocum), len(ocum) - 1, len_base, min_match, _u8ptr(out), cap,
        )
        if ln >= 0:
            return out[:ln].tobytes()
        cap *= 2


def lz_decode(blob, n_tokens, mcum, dcum, ocum, len_base, n_patterns, min_match):
    buf = np.frombuffer(blob, dtype=np.uint8).copy()
    mcum = np.ascontiguousarray(mcum, dtype=np.int32)
    dcum = np.ascontiguousarray(dcum, dtype=np.int32)
    ocum = np.ascontiguousarray(ocum, dtype=np.int32)
    kind = np.empty(n_tokens, dtype=np.int32)
    aval = np.empty(n_tokens, dtype=np.int64)
    bval = np.empty(n_tokens, dtype=np.int64)
    _lib.lz_decode(
        _u8ptr(buf), len(blob), n_tokens,
        _i32ptr(mcum), len(mcum) - 1, _i32ptr(dcum), len(dcum) - 1,
        _i32ptr(ocum), len(ocum) - 1, len_base, n_patterns, min_match,
        _i32ptr(kind), _ptr(aval), _ptr(bval),
    )
    return kind, aval, bval
