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


def _delta_apply(data, stride):
    out = bytearray(data)
    for i in range(stride, len(out)):
        out[i] = (data[i] - data[i - stride]) & 0xFF
    return bytes(out)


def _delta_invert(data, stride):
    out = bytearray(data)
    for i in range(stride, len(out)):
        out[i] = (out[i - stride] + data[i]) & 0xFF
    return bytes(out)


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


_OPS = {"delta": (_delta_apply, _delta_invert), "split": (_split_apply, _split_invert)}
_CODE = {"delta": 0, "split": 1}
_NAME = {0: "delta", 1: "split"}

# Candidate pipelines the training gate tries (a spec is a tuple of (op, arg)).
# Spans text (none), 8-bit and 16-bit numeric/image, and channel layouts.
TRANSFORM_SPECS = (
    (),
    (("delta", 1),),
    (("delta", 2),),
    (("delta", 4),),
    (("split", 2),),
    (("split", 2), ("delta", 1)),
    (("split", 2), ("delta", 2)),
    (("delta", 4), ("split", 2)),
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
