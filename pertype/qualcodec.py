"""Context-adaptive arithmetic coding of FASTQ quality scores.

Per-base Phred quality is a low-cardinality byte stream (raw ASCII 33..126, K=94)
with two strong contexts: **position** within the read (quality drifts along the
read) and the **previous quality value** (a base's confidence correlates with the
base before it). Each symbol is coded with an adaptive 94-symbol frequency model
selected by the context ``prevq * 64 + min(position, 63)``, where ``prevq`` is the
previous raw Phred byte (0 = start-of-read sentinel). On a real Illumina stream this
context model beats zstd -19 by ~+16% on the quality bytes (gzip's .fastq.gz form
by ~5.5x). Encoder and decoder evolve their counts identically as they go, so
nothing is transmitted. Pure Python (like the arithmetic coder it builds on) and
exactly reversible: ``decode(encode(q, lens)) == (q, lens)``.

Stream format: MAGIC + n total symbols (8 bytes big-endian) + zlib-compressed
read-lengths array (4-byte big-endian length prefix + zlib of big-endian uint32s) +
the arithmetic payload. An optional C/Rust twin (``pertype.native``) is
byte-identical to the pure-Python reference below.
"""
import zlib

from pertype.arithmetic import ArithmeticEncoder, ArithmeticDecoder
from pertype.bitio import BitReader

K = 94             # Phred symbols: raw ASCII 33..126
INCR = 24          # count increment per symbol (adaptation speed)
RESCALE = 1 << 14  # halve a context's counts when its total reaches this
POSCAP = 63        # within-read position clamp (context bucket)
MAGIC = b"QUAL"

# Optional native acceleration (byte-identical to the pure-Python reference
# below). Imported lazily so this module stays zero-dependency without numpy.
_native = None


def _get_native():
    global _native
    if _native is None:
        try:
            from pertype import native as n
            _native = n if n.HAVE_NATIVE else False
        except Exception:
            _native = False
    return _native


def _check(qual, lengths):
    if sum(lengths) != len(qual):
        raise ValueError("lengths do not sum to len(qual)")
    if qual and (min(qual) < 33 or max(qual) > 126):
        raise ValueError("quality bytes outside ASCII 33..126")


def _lengths_blob(lengths):
    return zlib.compress(b"".join(int(l).to_bytes(4, "big") for l in lengths), 9)


def _parse_lengths(blob):
    return [int.from_bytes(blob[i:i + 4], "big") for i in range(0, len(blob), 4)]


def encode(qual, lengths):
    """qual: raw Phred bytes (ASCII 33..126); lengths: per-read symbol counts."""
    qual = bytes(qual)
    lengths = [int(l) for l in lengths]
    _check(qual, lengths)
    nat = _get_native()
    payload = nat.qual_encode(qual, lengths) if nat else _encode_payload_py(qual, lengths)
    lblob = _lengths_blob(lengths)
    return (MAGIC + len(qual).to_bytes(8, "big")
            + len(lblob).to_bytes(4, "big") + lblob + payload)


def decode(blob):
    """Returns (qual: bytes, lengths: list of ints)."""
    if blob[:4] != MAGIC:
        raise ValueError("not a QUAL stream")
    n = int.from_bytes(blob[4:12], "big")
    zl = int.from_bytes(blob[12:16], "big")
    lengths = _parse_lengths(zlib.decompress(blob[16:16 + zl]))
    payload = blob[16 + zl:]
    nat = _get_native()
    qual = nat.qual_decode(payload, n, lengths) if nat else _decode_payload_py(payload, n, lengths)
    return qual, lengths


def _encode_py(qual, lengths):
    """Pure-Python reference encode — the full self-contained blob."""
    qual = bytes(qual)
    lengths = [int(l) for l in lengths]
    _check(qual, lengths)
    lblob = _lengths_blob(lengths)
    return (MAGIC + len(qual).to_bytes(8, "big")
            + len(lblob).to_bytes(4, "big") + lblob
            + _encode_payload_py(qual, lengths))


def _decode_py(blob):
    """Pure-Python reference decode — returns (qual: bytes, lengths)."""
    if blob[:4] != MAGIC:
        raise ValueError("not a QUAL stream")
    n = int.from_bytes(blob[4:12], "big")
    zl = int.from_bytes(blob[12:16], "big")
    lengths = _parse_lengths(zlib.decompress(blob[16:16 + zl]))
    return _decode_payload_py(blob[16 + zl:], n, lengths), lengths


def _encode_payload_py(qual, lengths):
    enc = ArithmeticEncoder()
    cnt, tot = {}, {}
    i, prevq = 0, 0
    for L in lengths:
        for p in range(L):
            ctx = prevq * 64 + (p if p < POSCAP else POSCAP)
            a = cnt.get(ctx)
            if a is None:
                a = [1] * K; cnt[ctx] = a; tot[ctx] = K
            s = qual[i] - 33
            cum = 0
            for j in range(s):
                cum += a[j]
            enc.encode(cum, a[s], tot[ctx])
            a[s] += INCR; tot[ctx] += INCR
            if tot[ctx] >= RESCALE:
                t = 0
                for j in range(K):
                    a[j] = (a[j] + 1) >> 1; t += a[j]
                tot[ctx] = t
            prevq = s + 33
            i += 1
        prevq = 0
    enc.finish()
    return enc.getvalue()


def _decode_payload_py(blob, n, lengths):
    dec = ArithmeticDecoder(BitReader(blob))
    cnt, tot = {}, {}
    out = bytearray(n)
    i, prevq = 0, 0
    for L in lengths:
        for p in range(L):
            ctx = prevq * 64 + (p if p < POSCAP else POSCAP)
            a = cnt.get(ctx)
            if a is None:
                a = [1] * K; cnt[ctx] = a; tot[ctx] = K
            target = dec.decode_target(tot[ctx])
            cum = 0; s = 0
            while cum + a[s] <= target:
                cum += a[s]; s += 1
            dec.update(cum, a[s], tot[ctx])
            a[s] += INCR; tot[ctx] += INCR
            if tot[ctx] >= RESCALE:
                t = 0
                for j in range(K):
                    a[j] = (a[j] + 1) >> 1; t += a[j]
                tot[ctx] = t
            out[i] = s + 33
            prevq = s + 33
            i += 1
        prevq = 0
    return bytes(out)
