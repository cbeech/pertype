"""Context-adaptive arithmetic coding of a prediction residual.

A memoryless Golomb/Rice coder assumes one fixed geometric residual
distribution. Real signals violate that: residual magnitude varies with local
activity (quiet passages vs transients in audio; baseline vs QRS in ECG), so a
single distribution leaves ~1 bit/sample on the table. This coder instead emits
each residual's magnitude **bucket** ``k = bit_length(zigzag(r))`` with an
adaptive frequency model **selected by the previous bucket** (the context), then
the ``k-1`` low mantissa bits raw (the leading 1 is implicit). Conditioning on
recent magnitude tracks the signal's time-varying entropy.

Encoder and decoder update their counts identically as they go, so nothing is
transmitted. Pure Python (like the arithmetic coder it builds on) and exactly
reversible: ``decode(encode(res), len(res)) == res``.
"""
from compressor.arithmetic import ArithmeticEncoder, ArithmeticDecoder
from compressor.bitio import BitReader

NB = 65            # buckets 0..64 — covers any int64 zigzag magnitude
INCR = 32          # count increment per symbol (adaptation speed)
RESCALE = 1 << 14  # halve a context's counts when its total reaches this


def _zigzag(r):
    return (r << 1) ^ (r >> 63)        # signed -> unsigned, |r| < 2**63


def _unzigzag(u):
    return (u >> 1) ^ -(u & 1)


def _new_model():
    # per-context: counts over the NB buckets, plus the running total
    return [[1] * NB for _ in range(NB)], [NB] * NB


def encode(res):
    """res: iterable of integer residuals -> bytes."""
    enc = ArithmeticEncoder()
    freq, tot = _new_model()
    ctx = 0
    for r in res:
        u = _zigzag(int(r))
        k = u.bit_length()
        f = freq[ctx]
        cum = 0
        for s in range(k):
            cum += f[s]
        enc.encode(cum, f[k], tot[ctx])
        if k >= 2:
            enc.encode_bits(u & ((1 << (k - 1)) - 1), k - 1)
        f[k] += INCR
        tot[ctx] += INCR
        if tot[ctx] >= RESCALE:
            t = 0
            for s in range(NB):
                f[s] = (f[s] + 1) >> 1
                t += f[s]
            tot[ctx] = t
        ctx = k
    enc.finish()
    return enc.getvalue()


def decode(blob, n):
    """Return a list of ``n`` integer residuals from ``blob``."""
    dec = ArithmeticDecoder(BitReader(blob))
    freq, tot = _new_model()
    ctx = 0
    out = []
    for _ in range(n):
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
            u = (1 << (k - 1)) | dec.decode_bits(k - 1)
        out.append(_unzigzag(u))
        f[k] += INCR
        tot[ctx] += INCR
        if tot[ctx] >= RESCALE:
            t = 0
            for s in range(NB):
                f[s] = (f[s] + 1) >> 1
                t += f[s]
            tot[ctx] = t
        ctx = k
    return out
