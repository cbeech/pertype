"""FASTQ whole-file codec (FQS1).

SPDX-License-Identifier: AGPL-3.0-or-later

Self-contained lossless container for 4-line FASTQ records. Three streams,
each coded to its own structure (the sweep's route-don't-unify lesson):

- **Headers** — a run template (alternating text / integer runs parsed from
  the first header) plus zigzag-varint deltas of each integer column, the
  resulting byte stream order-1-context arithmetic coded. Headers whose text
  runs deviate from the template are stored verbatim as exceptions. On
  instrument-generated names the integer columns advance by near-constant
  deltas, so this stream costs ~bits per record.
- **Sequences** — per-read reverse-complement orientation (reads arrive from
  both strands; orienting each read against the k-mers seen so far makes
  cross-read LZ matches strand-independent, measured -6% on real ENA data),
  then 2-bit packing (A/C/G/T, N positions delta-coded separately) and raw
  LZMA2 (liblzma preset 9). Orientation decisions are stored as a bitmap, so
  the decoder is deterministic and never re-runs the orientation heuristic.
- **Qualities** — the exact :mod:`pertype.qualcodec` payload (a (prev-quality,
  position) context model), reused verbatim so the C/Rust twins are shared.

A ``ctxblob`` is ``varint rawlen`` + the raw bytes coded with an adaptive
order-1 byte-context arithmetic model (256 contexts, increment 32, counts
halved when a context's total reaches 1<<14). Every section is length-prefixed
with a varint.

The codec rejects (``ValueError``) non-4-line input, seq/qual length
mismatches and quality bytes outside 33..126, so the auto router can fall
back to its generic path. ``decode(encode(x)) == x`` exactly.
"""
import lzma

from pertype import qualcodec
from pertype.arithmetic import ArithmeticEncoder, ArithmeticDecoder
from pertype.bitio import BitReader

MAGIC = b"FQS1"
KMER = 12                                   # orientation k-mer length
_LZMA_FILTERS = [{"id": lzma.FILTER_LZMA2, "preset": 9}]
_INCR, _RESCALE = 32, 1 << 14

_B2I = {65: 0, 67: 1, 71: 2, 84: 3}         # A C G T as byte values
_I2B = b"ACGT"
_RC = bytes.maketrans(b"ACGT", b"TGCA")     # complement (N and others map to themselves)
_KMASK = (1 << (2 * KMER)) - 1


# ----------------------------------------------------------------- varints --
def _wv(buf, n):
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            buf.append(b | 0x80)
        else:
            buf.append(b)
            return


def _rv(blob, pos):
    n = 0
    shift = 0
    while True:
        b = blob[pos]
        pos += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, pos
        shift += 7


def _zz(n):
    return (n << 1) if n >= 0 else ((-n) << 1) - 1


def _unzz(m):
    return (m >> 1) if not m & 1 else -((m + 1) >> 1)


# ----------------------------------------------------------------- ctxblob --
def _ctx_encode(data):
    enc = ArithmeticEncoder()
    cnt, tot = {}, {}
    prev = 0
    for b in data:
        a = cnt.get(prev)
        if a is None:
            a = [1] * 256
            cnt[prev] = a
            tot[prev] = 256
        cum = 0
        for j in range(b):
            cum += a[j]
        enc.encode(cum, a[b], tot[prev])
        a[b] += _INCR
        tot[prev] += _INCR
        if tot[prev] >= _RESCALE:
            t = 0
            for j in range(256):
                a[j] = (a[j] + 1) >> 1
                t += a[j]
            tot[prev] = t
        prev = b
    enc.finish()
    out = bytearray()
    _wv(out, len(data))
    return bytes(out) + enc.getvalue()


def _ctx_decode(blob):
    n, pos = _rv(blob, 0)
    dec = ArithmeticDecoder(BitReader(blob[pos:]))
    cnt, tot = {}, {}
    prev = 0
    out = bytearray()
    for _ in range(n):
        a = cnt.get(prev)
        if a is None:
            a = [1] * 256
            cnt[prev] = a
            tot[prev] = 256
        target = dec.decode_target(tot[prev])
        cum = 0
        s = 255
        for j in range(256):
            if cum + a[j] > target:
                s = j
                break
            cum += a[j]
        dec.update(cum, a[s], tot[prev])
        out.append(s)
        a[s] += _INCR
        tot[prev] += _INCR
        if tot[prev] >= _RESCALE:
            t = 0
            for j in range(256):
                a[j] = (a[j] + 1) >> 1
                t += a[j]
            tot[prev] = t
        prev = s
    return bytes(out)


# ----------------------------------------------------------------- headers --
def _parse_header(h):
    """Split a header into alternating text/integer runs.

    Returns (flags, txts, ints, iraws): per run a flag (1 = integer), the text
    runs verbatim, the integer values, and the integer runs' raw bytes (kept
    so leading-zero widths survive the round trip)."""
    flags, txts, ints, iraws = [], [], [], []
    i, n = 0, len(h)
    while i < n:
        j = i
        if 48 <= h[i] <= 57:
            while j < n and 48 <= h[j] <= 57:
                j += 1
            iraws.append(bytes(h[i:j]))
            ints.append(int(h[i:j]))
            flags.append(1)
        else:
            while j < n and not (48 <= h[j] <= 57):
                j += 1
            txts.append(bytes(h[i:j]))
            flags.append(0)
        i = j
    return tuple(flags), tuple(txts), tuple(ints), tuple(iraws)


def _padint(v, width):
    """Decimal form of ``v`` zero-padded to ``width`` (never truncated)."""
    t = str(v)
    return ("0" * (width - len(t)) + t) if len(t) < width else t


def _encode_headers(heads):
    flags, txts, ints, iraws = _parse_header(heads[0])
    tmpl = bytearray()
    _wv(tmpl, len(flags))
    ti = ii = 0
    for f in flags:
        tmpl.append(f)
        if not f:
            t = txts[ti]
            ti += 1
            _wv(tmpl, len(t))
            tmpl += t
        else:
            _wv(tmpl, len(iraws[ii]))
            ii += 1
    widths = [len(r) for r in iraws]
    dstream = bytearray()
    for v in ints:
        _wv(dstream, v)
    exc = bytearray()
    nexc = 0
    last_idx = 0
    prev = ints
    for idx in range(1, len(heads)):
        f2, t2, i2, r2 = _parse_header(heads[idx])
        if (f2 != flags or t2 != txts
                or any(_padint(v, w).encode() != raw
                       for v, w, raw in zip(i2, widths, r2))):
            nexc += 1
            _wv(exc, idx - last_idx)
            last_idx = idx
            h = heads[idx]
            _wv(exc, len(h))
            exc += h
            continue
        for a, b in zip(i2, prev):
            _wv(dstream, _zz(a - b))
        prev = i2
    return bytes(tmpl), _ctx_encode(bytes(dstream)), nexc, bytes(exc), len(ints)


def _decode_headers(tmpl, dblob, nexc, exc, ncols, nrec):
    pos = 0
    nruns, pos = _rv(tmpl, pos)
    flags, txts, widths = [], [], []
    for _ in range(nruns):
        f = tmpl[pos]
        pos += 1
        flags.append(f)
        if not f:
            L, pos = _rv(tmpl, pos)
            txts.append(tmpl[pos:pos + L])
            pos += L
        else:
            w, pos = _rv(tmpl, pos)
            widths.append(w)
    ddata = _ctx_decode(dblob)
    dpos = 0
    ints = []
    for _ in range(ncols):
        v, dpos = _rv(ddata, dpos)
        ints.append(v)
    exc_map = {}
    epos = 0
    last = 0
    for _ in range(nexc):
        d, epos = _rv(exc, epos)
        last += d
        L, epos = _rv(exc, epos)
        exc_map[last] = exc[epos:epos + L]
        epos += L
    heads = []
    prev = ints
    for idx in range(nrec):
        if idx in exc_map:
            heads.append(exc_map[idx])
            continue
        if idx > 0:
            nxt = []
            for b in prev:
                m, dpos = _rv(ddata, dpos)
                nxt.append(b + _unzz(m))
            prev = nxt
        parts = []
        ti = ii = 0
        for f in flags:
            if f:
                parts.append(_padint(prev[ii], widths[ii]).encode())
                ii += 1
            else:
                parts.append(txts[ti])
                ti += 1
        heads.append(b"".join(parts))
    return heads


# -------------------------------------------------------------- orientation --
def _kmers(s):
    """Yield 2-bit k-mer ints for ACGT-only runs (k-mers containing other
    bases are skipped, so the rule is trivially identical in every port)."""
    kmer = 0
    run = 0
    for b in s:
        v = _B2I.get(b)
        if v is None:
            kmer = 0
            run = 0
            continue
        kmer = ((kmer << 2) | v) & _KMASK
        run += 1
        if run >= KMER:
            yield kmer


def _orient(seqs):
    """Orient each read against the k-mers seen so far. Returns (oriented,
    bitmap-bytes MSB-first; bit i set = read i was reverse-complemented)."""
    seen = bytearray(1 << (2 * KMER))
    out = []
    bmp = bytearray()
    cur = 0
    nb = 0
    
    for s in seqs:
        hf = 0
        for k in _kmers(s):
            hf += seen[k]
        r = s.translate(_RC)[::-1]
        hr = 0
        for k in _kmers(r):
            hr += seen[k]
        use = r if hr > hf else s
        flip = 1 if hr > hf else 0
        cur = (cur << 1) | flip
        nb += 1
        if nb % 8 == 0:
            bmp.append(cur)
            cur = 0
        for k in _kmers(use):
            seen[k] = 1
        out.append(use)
    if nb % 8:
        bmp.append(cur << (8 - nb % 8))
    return out, bytes(bmp)


# --------------------------------------------------------------- sequences --
def _pack(seqs):
    """2-bit pack; N positions (global base index) collected separately."""
    out = bytearray()
    cur = 0
    nb = 0
    npos = []
    for s in seqs:
        for b in s:
            v = _B2I.get(b)
            if v is None:
                npos.append(nb)
                v = 0
            cur = (cur << 2) | v
            nb += 1
            if nb % 4 == 0:
                out.append(cur)
                cur = 0
    if nb % 4:
        out.append(cur << (2 * (4 - nb % 4)))
    return bytes(out), npos, nb


def _unpack(packed, nbase, lengths, npos):
    nset = set(npos)
    seqs = []
    off = 0
    for L in lengths:
        chars = bytearray(L)
        for j in range(L):
            g = off + j
            if g in nset:
                chars[j] = 78                    # 'N'
            else:
                chars[j] = _I2B[(packed[g // 4] >> (6 - 2 * (g % 4))) & 3]
        seqs.append(bytes(chars))
        off += L
    return seqs


def _unorient(seqs, bmp, nrec):
    out = []
    for i, s in enumerate(seqs):
        bit = (bmp[i // 8] >> (7 - i % 8)) & 1
        out.append(s.translate(_RC)[::-1] if bit else s)
    return out


def _deltas(vals):
    """Increasing non-negative ints -> forward deltas as varint bytes."""
    out = bytearray()
    prev = 0
    for v in vals:
        _wv(out, v - prev)
        prev = v
    return bytes(out)


def _undeltas(data):
    out = []
    pos = 0
    prev = 0
    while pos < len(data):
        d, pos = _rv(data, pos)
        prev += d
        out.append(prev)
    return out


def _zz_deltas(vals):
    out = bytearray()
    _wv(out, vals[0])
    for a, b in zip(vals[1:], vals[:-1]):
        _wv(out, _zz(a - b))
    return bytes(out)


def _zz_undeltas(data, n):
    out = []
    pos = 0
    v, pos = _rv(data, pos)
    out.append(v)
    for _ in range(n - 1):
        m, pos = _rv(data, pos)
        out.append(out[-1] + _unzz(m))
    return out


# -------------------------------------------------------------------- codec --
def encode(fastq):
    """Encode raw FASTQ bytes to the self-contained FQS1 blob."""
    lines = fastq.split(b"\n")
    trailing = lines[-1] == b""
    rec = lines[:-1] if trailing else lines
    if not rec or len(rec) % 4:
        raise ValueError("not 4-line FASTQ")
    heads = rec[0::4]
    seqs = rec[1::4]
    plus = rec[2::4]
    quals = rec[3::4]
    nrec = len(heads)
    lengths = [len(q) for q in quals]
    if any(len(s) != L for s, L in zip(seqs, lengths)):
        raise ValueError("seq/qual length mismatch")
    qflat = b"".join(quals)
    if qflat and (min(qflat) < 33 or max(qflat) > 126):
        raise ValueError("quality byte outside 33..126")

    tmpl, dblob, nexc, exc, ncols = _encode_headers(heads)
    lblob = _ctx_encode(_zz_deltas(lengths))
    if all(p == b"+" for p in plus):
        pflag, pblob = 0, b""
    else:
        praw = bytearray()
        for p in plus:
            _wv(praw, len(p))
            praw += p
        pflag, pblob = 1, _ctx_encode(bytes(praw))
    oseqs, bmp = _orient(seqs)
    packed, npos, nbase = _pack(oseqs)
    bblob = _ctx_encode(bmp)
    npblob = _ctx_encode(_deltas(npos))
    lz = lzma.compress(packed, format=lzma.FORMAT_RAW, filters=_LZMA_FILTERS)

    nat = qualcodec._get_native()
    if nat:
        qpayload = nat.qual_encode(qflat, lengths)
    else:
        qpayload = qualcodec._encode_payload_py(qflat, lengths)

    out = bytearray(MAGIC)
    out += nrec.to_bytes(8, "big")
    out.append(1 if trailing else 0)
    for section in (tmpl, dblob, lblob, bblob, npblob):
        _wv(out, len(section))
        out += section
    _wv(out, nexc)
    out += exc
    out.append(pflag)
    if pflag:
        _wv(out, len(pblob))
        out += pblob
    out += nbase.to_bytes(8, "big")
    _wv(out, len(lz))
    out += lz
    _wv(out, len(qpayload))
    out += qpayload
    _wv(out, ncols)
    return bytes(out)


def decode(blob):
    """Decode an FQS1 blob back to the exact original FASTQ bytes."""
    if blob[:4] != MAGIC:
        raise ValueError("not an FQS1 stream")
    nrec = int.from_bytes(blob[4:12], "big")
    trailing = blob[12]
    pos = 13
    sections = []
    for _ in range(5):
        L, pos = _rv(blob, pos)
        sections.append(blob[pos:pos + L])
        pos += L
    tmpl, dblob, lblob, bblob, npblob = sections
    nexc, pos = _rv(blob, pos)
    epos = pos
    for _ in range(nexc):
        _, epos = _rv(blob, epos)
        L, epos = _rv(blob, epos)
        epos += L
    exc = blob[pos:epos]
    pos = epos
    pflag = blob[pos]
    pos += 1
    plus = None
    if pflag:
        L, pos = _rv(blob, pos)
        praw = _ctx_decode(blob[pos:pos + L])
        pos += L
        plus = []
        ppos = 0
        while ppos < len(praw):
            L2, ppos = _rv(praw, ppos)
            plus.append(praw[ppos:ppos + L2])
            ppos += L2
    nbase = int.from_bytes(blob[pos:pos + 8], "big")
    pos += 8
    L, pos = _rv(blob, pos)
    packed = lzma.decompress(blob[pos:pos + L], format=lzma.FORMAT_RAW,
                             filters=_LZMA_FILTERS)
    pos += L
    L, pos = _rv(blob, pos)
    qpayload = blob[pos:pos + L]
    pos += L
    ncols, pos = _rv(blob, pos)
    if pos != len(blob):
        raise ValueError("trailing bytes after FQS1 payload")

    lengths = _zz_undeltas(_ctx_decode(lblob), nrec)
    heads = _decode_headers(tmpl, dblob, nexc, exc, ncols, nrec)
    bmp = _ctx_decode(bblob)
    npos = _undeltas(_ctx_decode(npblob))
    seqs = _unorient(_unpack(packed, nbase, lengths, npos), bmp, nrec)

    nat = qualcodec._get_native()
    if nat:
        qflat = nat.qual_decode(qpayload, sum(lengths), lengths)
    else:
        qflat = qualcodec._decode_payload_py(qpayload, sum(lengths), lengths)

    lines = []
    qi = 0
    for i in range(nrec):
        L2 = lengths[i]
        lines.append(heads[i])
        lines.append(seqs[i])
        lines.append(b"+" if plus is None else plus[i])
        lines.append(qflat[qi:qi + L2])
        qi += L2
    out = b"\n".join(lines)
    return out + b"\n" if trailing else out
