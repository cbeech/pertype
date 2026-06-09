"""Columnar codec for regular delimited-text tables (CSV / TSV / etc.).

A row-major table interleaves unlike values (a date, a voltage, a label) on every
line, which defeats both LZ and prediction. Transposing to **column-major** groups a
column's homogeneous values together, and then each column gets the strategy that
suits it:

  * **numeric** — cells that are consistently-formatted ints or fixed-decimals are
    scaled to integers and coded as delta + ``ctxcoder`` (the lever that already gives
    ~6x on this kind of sensor data), then reformatted canonically and checked to
    reproduce the original text exactly;
  * **text** — everything else (dates, times, labels) is deflated column-major, where
    the grouped values compress far better than row-interleaved.

Only a *regular* grid is transposed: one delimiter, a constant field count per row, a
consistent line terminator. Anything irregular (quoted fields that change the field
count, ragged rows) falls back to whole-stream deflate or store. The first row is kept
verbatim as a header so a column of numbers isn't poisoned by its name. The container
is self-describing and the grid path is **verified byte-exact at encode time**, so the
result is always lossless and never larger than the input.
"""
import zlib

import numpy as np

from pertype import ctxcoder

CMAGIC = b"CSV1"
M_STORE, M_DEFLATE, M_GRID = 0, 1, 2
_DELIMS = (b";", b",", b"\t", b"|")


def _u(x, k):
    return int(x).to_bytes(k, "big")


def _ru(b, p, k):
    return int.from_bytes(b[p:p + k], "big"), p + k


# --- numeric column detection / (de)coding -----------------------------------

def _fmt(v, ndec):
    """Canonical text of scaled integer ``v`` with ``ndec`` decimal places."""
    neg = v < 0
    a = -v if neg else v
    if ndec == 0:
        body = str(a)
    else:
        scale = 10 ** ndec
        body = f"{a // scale}.{a % scale:0{ndec}d}"
    return ("-" + body) if neg else body


def _parse_numeric(col):
    """If every cell is a canonical int/fixed-decimal with a *constant* decimal count,
    return (ndec, int64 values); else None. 'Canonical' = reformatting the parsed value
    reproduces the cell byte-for-byte (so leading zeros, '+', etc. fall through to text)."""
    first = col[0]
    if not first:
        return None
    try:
        s0 = first.decode("ascii")
    except UnicodeDecodeError:
        return None
    ndec = len(s0.split(".", 1)[1]) if "." in s0 else 0
    vals = np.empty(len(col), np.int64)
    scale = 10 ** ndec
    for i, c in enumerate(col):
        try:
            s = c.decode("ascii")
        except UnicodeDecodeError:
            return None
        neg = s.startswith("-")
        body = s[1:] if neg else s
        if ndec == 0:
            if not body.isdigit():
                return None
            v = int(body)
        else:
            if "." not in body:
                return None
            ip, fp = body.split(".", 1)
            if len(fp) != ndec or not ip.isdigit() or not fp.isdigit():
                return None
            v = int(ip) * scale + int(fp)
        if neg:
            v = -v
        if v.bit_length() > 62 or _fmt(v, ndec) != s:    # overflow guard + canonical check
            return None
        vals[i] = v
    return ndec, vals


def _code_idx(idx):
    """Smallest of raw / delta / Δ² of an int index field under ctxcoder; (sel, blob)."""
    d = idx.copy()
    d[1:] = idx[1:] - idx[:-1]
    dd = d.copy()
    dd[1:] = d[1:] - d[:-1]
    return min(((0, ctxcoder.encode(idx)), (1, ctxcoder.encode(d)),
               (2, ctxcoder.encode(dd))), key=lambda c: len(c[1]))


def _text_dict_block(col, delim):
    """Dictionary path for a text column: distinct cells (deflated, first-seen order) +
    a delta-coded index per row. Wins on low-cardinality, slowly-varying columns (dates,
    labels) that deflate handles poorly. Returns the block, or None if not worthwhile."""
    seen = {}
    distinct = []
    inv = np.empty(len(col), np.int64)
    for i, c in enumerate(col):
        j = seen.get(c)
        if j is None:
            j = len(distinct); seen[c] = j; distinct.append(c)
        inv[i] = j
    if len(distinct) >= len(col):                       # no repetition -> nothing to gain
        return None
    dz = zlib.compress(delim.join(distinct), 9)
    sel, iblob = _code_idx(inv)
    return (bytes([2]) + _u(len(distinct), 4) + _u(len(dz), 4) + dz
            + bytes([sel]) + _u(len(iblob), 4) + iblob)


def _encode_col(col, delim):
    """Smallest of: text-deflate, text-dictionary, or numeric (scaled-int delta) coding."""
    text = zlib.compress(delim.join(col), 9)
    cands = [bytes([0]) + _u(len(text), 4) + text]

    dict_blk = _text_dict_block(col, delim)
    if dict_blk is not None:
        cands.append(dict_blk)

    parsed = _parse_numeric(col)
    if parsed is not None:
        ndec, vals = parsed
        sel, blob = _code_idx(vals)
        cands.append(bytes([1, ndec, sel]) + _u(len(blob), 4) + blob)

    return min(cands, key=len)


def _decode_col(blob, p, n, delim):
    kind = blob[p]; p += 1
    if kind == 0:                                     # text: deflated cells
        ln, p = _ru(blob, p, 4)
        return zlib.decompress(blob[p:p + ln]).split(delim), p + ln
    if kind == 2:                                     # text: dictionary + index
        nu, p = _ru(blob, p, 4)
        dl, p = _ru(blob, p, 4)
        distinct = zlib.decompress(blob[p:p + dl]).split(delim); p += dl
        sel = blob[p]; p += 1
        il, p = _ru(blob, p, 4)
        idx = np.asarray(ctxcoder.decode(blob[p:p + il], n), np.int64)
        for _ in range(sel):
            idx = np.cumsum(idx)
        return [distinct[int(i)] for i in idx], p + il
    ndec = blob[p]; sel = blob[p + 1]; p += 2          # numeric: scaled-int delta
    ln, p = _ru(blob, p, 4)
    vals = np.asarray(ctxcoder.decode(blob[p:p + ln], n), np.int64)
    for _ in range(sel):                              # 0/1/2 cumulative sums undo raw/delta/Δ²
        vals = np.cumsum(vals)
    return [_fmt(int(v), ndec).encode("ascii") for v in vals], p + ln


# --- grid detection -----------------------------------------------------------

def _detect_grid(data):
    """(delim, lt, has_trailing_lt, header, rows) for a regular table, or None."""
    nl = data.count(b"\n")
    if nl < 3:
        return None
    lt = b"\r\n" if data.count(b"\r\n") == nl else b"\n"
    has_tl = data.endswith(lt)
    lines = data.split(lt)
    if has_tl:
        lines = lines[:-1]
    if len(lines) < 3:
        return None
    delim = None
    for d in _DELIMS:
        k = lines[0].count(d)
        if k >= 1 and all(ln.count(d) == k for ln in lines):
            delim = d
            break
    if delim is None:
        return None
    return delim, lt, has_tl, lines[0], lines[1:]


# --- public API ---------------------------------------------------------------

def _encode_grid(data):
    info = _detect_grid(data)
    if info is None:
        return None
    delim, lt, has_tl, header, rows = info
    n = len(rows)
    if n < 2:
        return None
    K = header.count(delim) + 1
    split = [r.split(delim) for r in rows]
    out = bytearray(CMAGIC + bytes([M_GRID]))
    out += bytes([delim[0], 0 if lt == b"\n" else 1, 1 if has_tl else 0])
    out += _u(n, 4) + _u(K, 2) + _u(len(header), 4) + header
    for c in range(K):
        out += _encode_col([row[c] for row in split], delim)
    return bytes(out)


def encode(data):
    """Compress a delimited-text table; returns a self-describing CSV1 container that
    is always lossless and never larger than ``data`` + a few bytes."""
    data = bytes(data)
    candidates = [CMAGIC + bytes([M_STORE]) + data,
                  CMAGIC + bytes([M_DEFLATE]) + zlib.compress(data, 9)]
    grid = _encode_grid(data)
    if grid is not None and decode(grid) == data:     # verify byte-exact before trusting it
        candidates.append(grid)
    return min(candidates, key=len)


def decode(blob):
    if blob[:4] != CMAGIC:
        raise ValueError("not a CSV1 stream")
    m = blob[4]
    if m == M_STORE:
        return blob[5:]
    if m == M_DEFLATE:
        return zlib.decompress(blob[5:])
    p = 5
    delim = bytes([blob[p]]); lt = b"\n" if blob[p + 1] == 0 else b"\r\n"
    has_tl = blob[p + 2] == 1; p += 3
    n, p = _ru(blob, p, 4)
    K, p = _ru(blob, p, 2)
    hl, p = _ru(blob, p, 4)
    header = blob[p:p + hl]; p += hl
    cols = []
    for _ in range(K):
        col, p = _decode_col(blob, p, n, delim)
        cols.append(col)
    rows = [delim.join(cols[c][r] for c in range(K)) for r in range(n)]
    return lt.join([header] + rows) + (lt if has_tl else b"")
