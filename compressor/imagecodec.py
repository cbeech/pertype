"""Lossless raw-image codec: 2D MED prediction + context-adaptive arithmetic coding.

The measure-first benchmarks (scripts/cr2_med_benchmark.py) showed this is the right
tool for continuous-tone sensor data (real Canon CR2 Bayer): on held-out raw, MED +
``ctxcoder`` reaches ~1.99x vs the generic LZ codec's 1.76x and PNG-16's 1.28x, and
crucially needs *no* trained model or dictionary — sensor noise has no exact repeats
for LZ to exploit, so prediction + a good entropy coder is all that helps. (Graphics,
which DO repeat, stay with the LZ+dictionary codec; see README.)

A Bayer (RGGB) sensor plane is deinterleaved into its 4 same-colour 2x2 phase
sub-planes first, so MED predicts each pixel from same-colour neighbours. Each plane's
MED residuals are coded independently with ``ctxcoder``. Decode replays the (native,
byte-identical) causal MED reconstruction. Byte-exact; a CRC guards the round-trip.

Container (big-endian)::

    magic     "RIMG"   4 bytes
    version   u8
    flags     u8       bit0 = Bayer 2x2 deinterleave
    height    u32
    width     u32
    crc32     u32      of the original uint16 little-endian bytes
    n_planes  u8
    planes    n_planes x (u32 length + ctxcoder blob)
"""
import zlib

import numpy as np

from compressor import ctxcoder, predictors

MAGIC = b"RIMG"
VERSION = 1
FLAG_BAYER = 1


def _plane_slices(bayer):
    if bayer:
        return [np.s_[0::2, 0::2], np.s_[0::2, 1::2], np.s_[1::2, 0::2], np.s_[1::2, 1::2]]
    return [np.s_[:, :]]


def encode(img, bayer=True):
    """Encode a 2D integer image plane (uint16, e.g. a Bayer sensor frame)."""
    img = np.ascontiguousarray(img)
    if img.ndim != 2:
        raise ValueError("imagecodec.encode expects a 2D array")
    H, W = img.shape
    src = img.astype(np.int32)
    parts = []
    for sl in _plane_slices(bayer):
        res = predictors.forward(np.ascontiguousarray(src[sl]), "med")
        parts.append(ctxcoder.encode(res.reshape(-1)))

    header = bytearray(MAGIC)
    header.append(VERSION)
    header.append(FLAG_BAYER if bayer else 0)
    header += int(H).to_bytes(4, "big")
    header += int(W).to_bytes(4, "big")
    header += (zlib.crc32(img.astype("<u2").tobytes()) & 0xFFFFFFFF).to_bytes(4, "big")
    header.append(len(parts))
    body = bytearray()
    for b in parts:
        body += len(b).to_bytes(4, "big")
        body += b
    return bytes(header) + bytes(body)


def decode(blob):
    """Decode an ``encode`` container back to the 2D uint16 image (byte-exact)."""
    if blob[:4] != MAGIC:
        raise ValueError("not a RIMG container")
    if blob[4] != VERSION:
        raise ValueError(f"unsupported RIMG version {blob[4]}")
    bayer = bool(blob[5] & FLAG_BAYER)
    H = int.from_bytes(blob[6:10], "big")
    W = int.from_bytes(blob[10:14], "big")
    crc = int.from_bytes(blob[14:18], "big")
    n_planes = blob[18]
    pos = 19

    out = np.zeros((H, W), dtype=np.int32)
    slices = _plane_slices(bayer)
    if len(slices) != n_planes:
        raise ValueError("plane count mismatch")
    for sl in slices:
        n = int.from_bytes(blob[pos:pos + 4], "big")
        pos += 4
        chunk = blob[pos:pos + n]
        pos += n
        target = out[sl]
        res = np.asarray(ctxcoder.decode(chunk, target.size), dtype=np.int32).reshape(target.shape)
        out[sl] = predictors.reconstruct(res, "med")

    img = out.astype("<u2")
    if (zlib.crc32(img.tobytes()) & 0xFFFFFFFF) != crc:
        raise ValueError("checksum mismatch — corrupt data")
    return np.asarray(img)
