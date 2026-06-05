"""Columnar codec for fixed-width binary record streams (LiDAR LAS, star catalogs,
any binary table).

A record of ``W`` bytes interleaves several fixed-width fields. Consecutive records
are usually spatially or temporally local, so **de-interleaving the records into
per-field columns and first-differencing the numeric ones** decorrelates far better
than a flat byte pass — exactly the lever that wins on LiDAR point clouds (int32
X/Y/Z deltas) where general codecs see only interleaved noise.

A *schema* is a list of field byte-widths (each in {1, 2, 4}) summing to W. Each field
becomes a column of little-endian unsigned integers; per column we keep the smaller of
``ctxcoder(raw)`` vs ``ctxcoder(first-difference)``. The caller can pass an exact schema
(e.g. parsed from a LAS header); otherwise :func:`encode` searches uniform tilings
(all-1 / all-2 / all-4, where the width divides) and keeps the smallest. The chosen
schema is recorded in a self-describing container so :func:`decode` is byte-exact, and
a *store* fallback guarantees the output is never larger than the input (plus a 5-byte
header).
"""
import numpy as np

from compressor import ctxcoder

CMAGIC = b"COL1"
M_STORE, M_COL = 0, 1
_ELEMS = (4, 2, 1)            # field widths tried for an auto (uniform) schema
_MAXW = 4                     # widest field handled directly (fits int64 without overflow)


# --- column <-> bytes (vectorised) -------------------------------------------

def _deinterleave(body, n, schema):
    """(n*W bytes, schema) -> list of int64 column arrays, one per field."""
    W = sum(schema)
    mat = np.frombuffer(body, np.uint8).reshape(n, W)
    cols, off = [], 0
    for w in schema:
        c = np.zeros(n, np.int64)
        for b in range(w):
            c += mat[:, off + b].astype(np.int64) << (8 * b)
        cols.append(c)
        off += w
    return cols


def _interleave(cols, n, schema):
    """Inverse of :func:`_deinterleave`: columns -> the original record bytes."""
    W = sum(schema)
    mat = np.empty((n, W), np.uint8)
    off = 0
    for c, w in zip(cols, schema):
        for b in range(w):
            mat[:, off + b] = ((c >> (8 * b)) & 0xFF).astype(np.uint8)
        off += w
    return mat.reshape(-1).tobytes()


def _code_col(col):
    """Smaller of raw vs first-difference under ctxcoder. Returns (selector, blob)."""
    raw = ctxcoder.encode(col)
    d = col.copy()
    d[1:] = col[1:] - col[:-1]
    delta = ctxcoder.encode(d)
    return (1, delta) if len(delta) < len(raw) else (0, raw)


# --- container framing --------------------------------------------------------

def _u(x, k):
    return int(x).to_bytes(k, "big")


def _ru(b, p, k):
    return int.from_bytes(b[p:p + k], "big"), p + k


def _pack(schema, n, trailing, coded):
    out = bytearray(CMAGIC + bytes([M_COL]))
    out += bytes([len(schema)]) + bytes(schema) + _u(n, 4) + _u(len(trailing), 2) + trailing
    for sel, blob in coded:
        out += bytes([sel]) + _u(len(blob), 4) + blob
    return bytes(out)


# --- public API ---------------------------------------------------------------

def _try_schema(data, schema):
    """Encode ``data`` with an explicit field schema, or None if it doesn't apply."""
    W = sum(schema)
    if W < 1 or any(w < 1 or w > _MAXW for w in schema):
        return None
    n = len(data) // W
    if n < 2:
        return None
    body, trailing = data[:n * W], data[n * W:]
    coded = [_code_col(c) for c in _deinterleave(body, n, schema)]
    return _pack(schema, n, trailing, coded)


def encode(data, width=None, schema=None):
    """Compress a fixed-width record stream. Pass ``schema`` (list of field widths) for
    exact field columns, or ``width`` to search uniform tilings, or neither to detect a
    width. Always returns a COL1 container; never larger than ``data`` + 5 bytes."""
    data = bytes(data)
    candidates = []
    if schema is not None:
        candidates.append(list(schema))
    else:
        W = width or detect_width(data)
        if W and W >= 2:
            candidates += [[e] * (W // e) for e in _ELEMS if W % e == 0]

    best = None
    for sc in candidates:
        blob = _try_schema(data, sc)
        if blob is not None and (best is None or len(blob) < len(best)):
            best = blob

    store = CMAGIC + bytes([M_STORE]) + data
    return store if best is None or len(best) >= len(store) else best


def decode(blob):
    if blob[:4] != CMAGIC:
        raise ValueError("not a COL1 stream")
    if blob[4] == M_STORE:
        return blob[5:]
    p = 5
    nf = blob[p]; p += 1
    schema = list(blob[p:p + nf]); p += nf
    n, p = _ru(blob, p, 4)
    tl, p = _ru(blob, p, 2)
    trailing = blob[p:p + tl]; p += tl
    cols = []
    for _ in range(nf):
        sel = blob[p]; p += 1
        ln, p = _ru(blob, p, 4)
        vals = np.asarray(ctxcoder.decode(blob[p:p + ln], n), np.int64); p += ln
        cols.append(np.cumsum(vals) if sel else vals)
    return _interleave(cols, n, schema) + trailing


def detect_width(data, lo=2, hi=256, sample=1 << 16):
    """Dominant record period by byte autocorrelation, or 0 if no clear periodicity
    (so random / non-record data falls back to *store* and never expands)."""
    a = np.frombuffer(data[:sample], np.uint8).astype(np.int16)
    if len(a) < 64:
        return 0
    hi = min(hi, len(a) // 4)
    best_p, best = 0, 0.0
    for p in range(lo, hi + 1):
        m = float(np.mean(a[p:] == a[:-p]))
        if m > best:
            best, best_p = m, p
    return best_p if best > 0.2 else 0       # require a genuine periodic signal
