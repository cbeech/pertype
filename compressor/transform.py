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
            from compressor import native as n
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
}
_CODE = {"delta": 0, "split": 1, "xor": 2}
_NAME = {0: "delta", 1: "split", 2: "xor"}

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
)


def apply(data, spec):
    for op, arg in spec:
        data = _OPS[op][0](data, arg)
    return data


def invert(data, spec):
    for op, arg in reversed(spec):
        data = _OPS[op][1](data, arg)
    return data


def select(samples, cap=1 << 21):
    """Pick the transform that most shrinks the data under a fast zlib proxy.

    zlib measures decorrelation cheaply; the spec that helps it helps our coder
    too (less residual entropy). Returns the best spec (``()`` = identity)."""
    blob = b"".join(samples)
    if len(blob) > cap:
        blob = blob[:cap]
    if not blob:
        return ()
    best_spec, best_size = (), None
    for spec in TRANSFORM_SPECS:
        size = len(zlib.compress(apply(blob, spec), 6))
        if best_size is None or size < best_size:
            best_spec, best_size = spec, size
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
