"""Context-adaptive arithmetic coding of a prediction residual.

A memoryless Golomb/Rice coder assumes one fixed geometric residual
distribution. Real signals violate that: residual magnitude varies with local
activity (quiet passages vs transients in audio; baseline vs QRS in ECG), so a
single distribution leaves ~1 bit/sample on the table. This coder instead emits
each residual's magnitude **bucket** ``k = bit_length(zigzag(r))`` with an
adaptive frequency model **selected by the previous two buckets** (the context).
Conditioning on recent magnitude tracks the signal's time-varying entropy; an
order-2 context (vs order-1) measurably lowers the residual's conditional entropy —
e.g. on ECG it improves the ratio ~3.5% (5.15 → 4.97 b/s). The context bucket is
clamped, so the model stays dense enough to adapt.

Within a bucket the **top mantissa bit** is then coded with its own adaptive binary
model, indexed by (context, k) — for prediction residuals it is *not* uniform, so
modelling it lowers the rate a further **+0.4% to +4%** on numeric/columnar streams
(e.g. LiDAR coordinate deltas +4%). The remaining ``k-2`` low bits stay raw (the
leading 1 is implicit). Earlier this gain looked like ~0.7%; conditioning the bit on
(context, k) rather than alone is what makes it worth the small extra model.

Encoder and decoder update their counts identically as they go, so nothing is
transmitted. Pure Python (like the arithmetic coder it builds on) and exactly
reversible: ``decode(encode(res), len(res)) == res``.
"""
from compressor.arithmetic import ArithmeticEncoder, ArithmeticDecoder
from compressor.bitio import BitReader

NB = 65            # buckets 0..64 — covers any int64 zigzag magnitude
CTX_CLAMP = 16     # buckets clamped to this for the context (keeps it dense)
NCTX = (CTX_CLAMP + 1) ** 2   # order-2 context: (prev bucket, prev-prev bucket)
INCR = 32          # count increment per symbol (adaptation speed)
RESCALE = 1 << 14  # halve a context's counts when its total reaches this
MINCR = 24         # adaptation of the modelled top-mantissa bit
MRESCALE = 1 << 13


def _ctx(pk, pk2):
    a = pk if pk < CTX_CLAMP else CTX_CLAMP
    b = pk2 if pk2 < CTX_CLAMP else CTX_CLAMP
    return a * (CTX_CLAMP + 1) + b

# Optional native acceleration (byte-identical to the pure-Python reference
# below). Imported lazily so this module stays zero-dependency without numpy.
_native = None


def _get_native():
    global _native
    if _native is None:
        try:
            from compressor import native as n
            _native = n if n.HAVE_NATIVE else False
        except Exception:
            _native = False
    return _native


def _zigzag(r):
    return (r << 1) ^ (r >> 63)        # signed -> unsigned, |r| < 2**63


def _unzigzag(u):
    return (u >> 1) ^ -(u & 1)


def _new_model():
    # per-context: counts over the NB buckets, plus the running total
    return [[1] * NB for _ in range(NCTX)], [NB] * NCTX


def _new_mant():
    # adaptive binary model for the top mantissa bit, indexed by (ctx*NB + k)
    return [[1, 1] for _ in range(NCTX * NB)], [2] * (NCTX * NB)


def encode(res):
    """res: iterable of integer residuals -> bytes."""
    nat = _get_native()
    if nat:
        return nat.ctx_encode(res)
    return _encode_py(res)


def decode(blob, n):
    """Return a list of ``n`` integer residuals from ``blob``."""
    nat = _get_native()
    if nat:
        return nat.ctx_decode(blob, n).tolist()
    return _decode_py(blob, n)


def _encode_py(res):
    enc = ArithmeticEncoder()
    freq, tot = _new_model()
    mfreq, mtot = _new_mant()
    pk = pk2 = 0
    for r in res:
        u = _zigzag(int(r))
        k = u.bit_length()
        ctx = _ctx(pk, pk2)
        f = freq[ctx]
        cum = 0
        for s in range(k):
            cum += f[s]
        enc.encode(cum, f[k], tot[ctx])
        if k >= 2:
            mant = u & ((1 << (k - 1)) - 1)
            b1 = (mant >> (k - 2)) & 1                 # top mantissa bit: adaptively coded
            mi = ctx * NB + k
            m = mfreq[mi]
            enc.encode(0 if b1 == 0 else m[0], m[b1], mtot[mi])
            m[b1] += MINCR
            mtot[mi] += MINCR
            if mtot[mi] >= MRESCALE:
                m[0] = (m[0] + 1) >> 1
                m[1] = (m[1] + 1) >> 1
                mtot[mi] = m[0] + m[1]
            if k >= 3:                                 # remaining low bits stay raw
                enc.encode_bits(mant & ((1 << (k - 2)) - 1), k - 2)
        f[k] += INCR
        tot[ctx] += INCR
        if tot[ctx] >= RESCALE:
            t = 0
            for s in range(NB):
                f[s] = (f[s] + 1) >> 1
                t += f[s]
            tot[ctx] = t
        pk2 = pk
        pk = k
    enc.finish()
    return enc.getvalue()


def _decode_py(blob, n):
    dec = ArithmeticDecoder(BitReader(blob))
    freq, tot = _new_model()
    mfreq, mtot = _new_mant()
    pk = pk2 = 0
    out = []
    for _ in range(n):
        ctx = _ctx(pk, pk2)
        f = freq[ctx]
        target = dec.decode_target(tot[ctx])
        cum = 0
        k = 0
        while cum + f[k] <= target:
            cum += f[k]
            k += 1
        dec.update(cum, f[k], tot[ctx])
        if k == 0:
            u = 0
        elif k == 1:
            u = 1
        else:
            mi = ctx * NB + k
            m = mfreq[mi]
            b1 = 1 if dec.decode_target(mtot[mi]) >= m[0] else 0
            dec.update(0 if b1 == 0 else m[0], m[b1], mtot[mi])
            m[b1] += MINCR
            mtot[mi] += MINCR
            if mtot[mi] >= MRESCALE:
                m[0] = (m[0] + 1) >> 1
                m[1] = (m[1] + 1) >> 1
                mtot[mi] = m[0] + m[1]
            low = dec.decode_bits(k - 2) if k >= 3 else 0
            u = (1 << (k - 1)) | (b1 << (k - 2)) | low
        out.append(_unzigzag(u))
        f[k] += INCR
        tot[ctx] += INCR
        if tot[ctx] >= RESCALE:
            t = 0
            for s in range(NB):
                f[s] = (f[s] + 1) >> 1
                t += f[s]
            tot[ctx] = t
        pk2 = pk
        pk = k
    return out
