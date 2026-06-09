"""Lossless codec for low-cardinality float arrays (fixed-precision scientific grids).

Smooth float32/float64 fields — weather/climate grids, simulation output, quantised
sensor data — are stored at limited precision, so although the *bytes* look noisy (the
mantissa defeats prediction and XOR-delta), the array holds **few distinct values**
(e.g. a 5.4 M-cell temperature grid has only ~6.8 K distinct float32s). General codecs
get part of this via LZ matches; we capture it directly and losslessly:

  * map each value's exact bit pattern to an index into the sorted distinct values
    (a dictionary) — byte-exact, so -0.0 / NaN survive;
  * the index field is spatially smooth (neighbouring cells have nearby values), so it
    codes well as raw / first / second difference under ``ctxcoder`` (keep the smallest);
  * the dictionary (tiny — well under 1% of the output) is deflated.

This beats xz on exactly the smooth-fixed-precision-float case that was the standing
boundary (weather +34% over xz). When cardinality is high (genuinely noisy floats) the
dictionary path loses and a *store* fallback keeps the result no larger than the input.
"""
import zlib

import numpy as np

from pertype import ctxcoder

FMAGIC = b"FLT1"
M_STORE, M_DICT = 0, 1


def _u(x):
    return int(x).to_bytes(4, "big")


def _ru(b, p):
    return int.from_bytes(b[p:p + 4], "big"), p + 4


def _code_idx(idx):
    """Smallest of raw / delta / Δ² of the index field under ctxcoder; (selector, blob)."""
    d = idx.copy()
    d[1:] = idx[1:] - idx[:-1]
    dd = d.copy()
    dd[1:] = d[1:] - d[:-1]
    cands = [(0, ctxcoder.encode(idx)), (1, ctxcoder.encode(d)), (2, ctxcoder.encode(dd))]
    return min(cands, key=lambda c: len(c[1]))


def encode(data, itemsize):
    """Compress a raw little-endian float byte stream (itemsize 4 or 8). Returns a FLT1
    container, never larger than the input + a small header."""
    data = bytes(data)
    store = FMAGIC + bytes([M_STORE]) + data
    if itemsize not in (4, 8):
        return store
    n = len(data) // itemsize
    if n < 8:
        return store
    body, trailing = data[:n * itemsize], data[n * itemsize:]
    bits = np.frombuffer(body, f"<u{itemsize}")
    uniq, inv = np.unique(bits, return_inverse=True)   # uniq sorted; inv -> index per value
    sel, iblob = _code_idx(inv.astype(np.int64).ravel())
    dz = zlib.compress(uniq.tobytes(), 9)
    out = bytearray(FMAGIC + bytes([M_DICT, itemsize]))
    out += _u(n) + _u(len(trailing)) + trailing
    out += _u(len(uniq)) + _u(len(dz)) + dz
    out += bytes([sel]) + _u(len(iblob)) + iblob
    return bytes(out) if len(out) < len(store) else store


def decode(blob):
    if blob[:4] != FMAGIC:
        raise ValueError("not a FLT1 stream")
    if blob[4] == M_STORE:
        return blob[5:]
    itemsize = blob[5]; p = 6
    n, p = _ru(blob, p)
    tl, p = _ru(blob, p)
    trailing = blob[p:p + tl]; p += tl
    nu, p = _ru(blob, p)
    dl, p = _ru(blob, p)
    uniq = np.frombuffer(zlib.decompress(blob[p:p + dl]), f"<u{itemsize}"); p += dl
    sel = blob[p]; p += 1
    il, p = _ru(blob, p)
    inv = np.asarray(ctxcoder.decode(blob[p:p + il], n), np.int64)
    for _ in range(sel):
        inv = np.cumsum(inv)
    return uniq[inv.astype(np.intp)].tobytes() + trailing
