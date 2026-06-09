"""Reversible byte-stream transforms, selected per file type.

A transform *decorrelates* structured data before compression, so the entropy
coder has far less to encode. Crucially these are **generic** — they operate on
opaque bytes parameterized only by a stride, with no knowledge of the data's
shape — yet capture most of the gain a domain-specific predictor would. On 16-bit
raw images, `split(2) then delta(2)` reaches 2.27x where the untransformed data
manages 1.64x, because byte-plane splitting separates the low/high bytes of each
sample and the stride delta predicts each value from its same-position neighbour.

The transform that wins is data-dependent (delta for numeric/sensor/image data,
none for text), so training selects it on a fast proxy and records it in the
model. Every op is exactly reversible: ``invert(apply(x)) == x``.
"""
import zlib

# Optional native acceleration. Imported lazily so the core stays zero-dependency:
# if numpy/native is unavailable, the pure-Python paths below are used. Native is
# required to be byte-identical, so a file is interchangeable across both.
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


def _delta_apply_py(data, stride):
    out = bytearray(data)
    for i in range(stride, len(out)):
        out[i] = (data[i] - data[i - stride]) & 0xFF
    return bytes(out)


def _delta_invert_py(data, stride):
    out = bytearray(data)
    for i in range(stride, len(out)):
        out[i] = (out[i - stride] + data[i]) & 0xFF
    return bytes(out)


def _delta_apply(data, stride):
    nat = _get_native()
    return nat.delta_fwd(data, stride) if nat else _delta_apply_py(data, stride)


def _delta_invert(data, stride):
    nat = _get_native()
    return nat.delta_inv(data, stride) if nat else _delta_invert_py(data, stride)


def _xor_apply_py(data, stride):
    out = bytearray(data)
    for i in range(stride, len(out)):
        out[i] ^= data[i - stride]
    return bytes(out)


def _xor_invert_py(data, stride):
    out = bytearray(data)
    for i in range(stride, len(out)):
        out[i] ^= out[i - stride]
    return bytes(out)


def _xor_apply(data, stride):
    """Stride XOR-delta (Gorilla-style): XOR each value's bytes with the previous
    value's. Slowly-changing IEEE-754 floats leave mostly-zero bytes — which the
    LZ + ctxcoder stages then crush — where integer ``delta`` is useless (floats
    don't subtract meaningfully in byte space)."""
    try:
        import numpy as np
        a = np.frombuffer(data, dtype=np.uint8)
        out = a.copy()
        out[stride:] ^= a[:-stride]
        return out.tobytes()
    except Exception:
        return _xor_apply_py(data, stride)


def _xor_invert(data, stride):
    try:
        import numpy as np
        a = np.frombuffer(data, dtype=np.uint8)
        out = np.empty_like(a)
        for j in range(stride):
            out[j::stride] = np.bitwise_xor.accumulate(a[j::stride])
        return out.tobytes()
    except Exception:
        return _xor_invert_py(data, stride)


# FCM/DFCM value prediction for float64 (FPC, Burtscher & Ratanaworabhan). Two
# context predictors run in lockstep on encode and decode:
#   * FCM   predicts the next value from a table indexed by a hash of recent values
#           (learns recurring values / sequences);
#   * DFCM  predicts the next *difference* the same way (learns recurring deltas:
#           linear ramps, periodic signals).
# Per value we XOR the true bits with the better prediction (more leading-zero
# bytes) and emit a 1-byte selector + the residual. A good prediction leaves the
# high (sign/exponent) bytes zero; byte-plane-splitting the residuals clusters
# those zeros into long runs the LZ + ctxcoder stages then crush. The predictors
# are causal, so decode reconstructs each value then updates its tables identically.
_U64 = 0xFFFFFFFFFFFFFFFF


def _lead_zero_bytes(r):
    """Number of zero high bytes of a 64-bit value (little-endian: bytes 7..0)."""
    if r == 0:
        return 8
    c = 0
    for p in range(7, -1, -1):
        if (r >> (8 * p)) & 0xFF:
            break
        c += 1
    return c


def _fcm_apply(data, bits):
    n = len(data)
    nval = n // 8
    rem = n % 8
    mask = (1 << bits) - 1
    fcm = [0] * (1 << bits)
    dfcm = [0] * (1 << bits)
    fh = dh = last = 0
    mv = memoryview(data)
    sel = bytearray(nval)
    res = [0] * nval
    for i in range(nval):
        v = int.from_bytes(mv[i * 8:i * 8 + 8], "little")
        pf = fcm[fh]
        pd = (last + dfcm[dh]) & _U64
        rf = v ^ pf
        rd = v ^ pd
        if _lead_zero_bytes(rd) > _lead_zero_bytes(rf):
            sel[i] = 1
            res[i] = rd
        else:
            res[i] = rf
        fcm[fh] = v
        diff = (v - last) & _U64
        dfcm[dh] = diff
        fh = ((fh << 6) ^ (v >> 48)) & mask
        dh = ((dh << 2) ^ (diff >> 40)) & mask
        last = v
    # byte-plane layout: plane p holds byte p of every residual (high planes ~zero)
    planes = bytearray(8 * nval)
    for i in range(nval):
        r = res[i]
        for p in range(8):
            planes[p * nval + i] = (r >> (8 * p)) & 0xFF
    return bytes(sel) + bytes(planes) + bytes(mv[nval * 8:])


def _fcm_invert(data, bits):
    L = len(data)
    nval = L // 9          # L = nval (selectors) + 8*nval (planes) + rem, rem < 8 < 9
    mask = (1 << bits) - 1
    fcm = [0] * (1 << bits)
    dfcm = [0] * (1 << bits)
    fh = dh = last = 0
    sel = data[:nval]
    planes = data[nval:nval + 8 * nval]
    trailing = data[nval + 8 * nval:]
    out = bytearray(8 * nval)
    for i in range(nval):
        r = 0
        for p in range(8):
            r |= planes[p * nval + i] << (8 * p)
        pf = fcm[fh]
        pd = (last + dfcm[dh]) & _U64
        v = (r ^ (pd if sel[i] else pf)) & _U64
        out[i * 8:i * 8 + 8] = v.to_bytes(8, "little")
        fcm[fh] = v
        diff = (v - last) & _U64
        dfcm[dh] = diff
        fh = ((fh << 6) ^ (v >> 48)) & mask
        dh = ((dh << 2) ^ (diff >> 40)) & mask
        last = v
    return bytes(out) + bytes(trailing)


def _split_apply(data, n):
    # Deinterleave into n byte-planes (positions 0,n,2n.. then 1,n+1.. etc.).
    return b"".join(bytes(data[i::n]) for i in range(n))


def _split_invert(data, n):
    total = len(data)
    out = bytearray(total)
    pos = 0
    for i in range(n):
        length = len(range(i, total, n))
        out[i::n] = data[pos:pos + length]
        pos += length
    return bytes(out)


_OPS = {
    "delta": (_delta_apply, _delta_invert),
    "split": (_split_apply, _split_invert),
    "xor": (_xor_apply, _xor_invert),
    "fcm": (_fcm_apply, _fcm_invert),
}
_CODE = {"delta": 0, "split": 1, "xor": 2, "fcm": 3}
_NAME = {0: "delta", 1: "split", 2: "xor", 3: "fcm"}

# Candidate pipelines the training gate tries (a spec is a tuple of (op, arg)).
# Spans text (none), 8-bit and 16-bit numeric/image, channel layouts, and — via
# the xor + stride-8/4 specs — IEEE-754 float64/float32 (Gorilla XOR-delta, then
# byte-plane split so the near-constant sign/exponent planes compress).
TRANSFORM_SPECS = (
    (),
    (("delta", 1),),
    (("delta", 2),),
    (("delta", 4),),
    (("split", 2),),
    (("split", 2), ("delta", 1)),
    (("split", 2), ("delta", 2)),
    (("delta", 4), ("split", 2)),
    (("xor", 8),),
    (("xor", 8), ("split", 8)),
    (("split", 8),),
    (("xor", 4), ("split", 4)),
    (("fcm", 16),),
)


def apply(data, spec):
    for op, arg in spec:
        data = _OPS[op][0](data, arg)
    return data


def invert(data, spec):
    for op, arg in reversed(spec):
        data = _OPS[op][1](data, arg)
    return data


# The FCM/DFCM predictor is O(n) pure-Python, so ranking it on the full proxy blob
# would tax every type's training (even text/audio, where it never wins). Rank it on
# a smaller sample instead and compare by bytes-out-per-byte-in, so the comparison
# stays fair across the different sample sizes.
_SLOW_OPS = {"fcm"}
_SLOW_CAP = 1 << 18


def select(samples, cap=1 << 21):
    """Pick the transform that most shrinks the data under a fast zlib proxy.

    zlib measures decorrelation cheaply; the spec that helps it helps our coder
    too (less residual entropy). Returns the best spec (``()`` = identity).

    Cheap specs are ranked on the full proxy blob by compressed size (unchanged).
    The O(n) pure-Python FCM/DFCM specs are then judged against the incumbent on an
    *identical* smaller sample — fair (same bytes) and fast (FCM never taxes the
    training of types it can't win, like text/audio)."""
    blob = b"".join(samples)
    if len(blob) > cap:
        blob = blob[:cap]
    if not blob:
        return ()

    def zsize(spec, b):
        return len(zlib.compress(apply(b, spec), 6))

    cheap = [s for s in TRANSFORM_SPECS if not any(op in _SLOW_OPS for op, _ in s)]
    slow = [s for s in TRANSFORM_SPECS if any(op in _SLOW_OPS for op, _ in s)]

    best_spec, best_size = (), None
    for spec in cheap:
        size = zsize(spec, blob)
        if best_size is None or size < best_size:
            best_spec, best_size = spec, size

    if slow:
        sample = blob[:_SLOW_CAP]
        incumbent = zsize(best_spec, sample)   # the current winner on the same sample
        for spec in slow:
            size = zsize(spec, sample)
            if size < incumbent:
                best_spec, incumbent = spec, size
    return best_spec


def serialize(spec):
    out = bytearray([len(spec)])
    for op, arg in spec:
        out += bytes([_CODE[op], arg])
    return bytes(out)


def deserialize(blob):
    n = blob[0]
    pos = 1
    spec = []
    for _ in range(n):
        spec.append((_NAME[blob[pos]], blob[pos + 1]))
        pos += 2
    return tuple(spec)
