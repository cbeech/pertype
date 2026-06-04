"""Lossless raw/photo image codec: 2D MED prediction + context-adaptive arithmetic.

The measure-first benchmarks (scripts/cr2_med_benchmark.py, image_med_benchmark.py)
showed prediction — not LZ — is the right tool for continuous-tone images:

* **Bayer raw** (Canon CR2): deinterleave the RGGB mosaic into 4 same-colour 2x2
  sub-planes, MED-predict each. ~2.12x on full frames, beating Canon's own lossless.
* **RGB photo**: a reversible green-subtract colour transform (G, R-G, B-G) decorrelates
  the channels, then MED per plane. ~2.34x on full-res photo crops, beating PNG (2.09x).
* **gray**: a single MED plane.

No LZ, no trained model (sensor/photo noise has no exact repeats for LZ; prediction +
adaptive arithmetic is what helps). Decode replays the native, byte-identical causal
MED reconstruction. Byte-exact; a CRC guards the round-trip.

Container (big-endian)::

    magic     "RIMG"   4 bytes
    version   u8
    mode      u8       0 = gray (1 plane), 1 = Bayer (4 sub-planes), 2 = RGB (3 planes)
    itemsize  u8       bytes per sample (1 or 2)
    height    u32
    width     u32
    crc32     u32      of the original array's C-order bytes
    n_planes  u8
    planes    n_planes x (u32 length + ctxcoder blob)
"""
import zlib

import numpy as np

from compressor import ctxcoder, predictors

MAGIC = b"RIMG"
VERSION = 3
GRAY, BAYER, RGB = 0, 1, 2

# Per-plane predictor selection: each plane is coded with whichever predictor gives
# the smallest residual (a 1-byte selector per plane). MED is near-optimal on most
# planes (and has the fastest native reconstruction); GAP (CALIC) wins on the smooth
# same-colour Bayer sub-planes (+2.3% on full-frame raw). Paeth (code 1) was measured
# and never won a plane, so it's not in the shipped set — but decode still honours the
# selector value, so it could be re-enabled without a format change.
_PREDICTORS = [(0, "med"), (2, "gap")]
_KIND = {0: "med", 1: "paeth", 2: "gap"}


def _scale(itemsize):
    """GAP threshold scale: ~1 for 8-bit, ~64 for 16-bit, so the gradient tests
    track the value range."""
    return 64 if itemsize == 2 else 1


def _split(src, mode):
    """Forward decorrelation into a list of 2D int32 planes to MED+code."""
    if mode == BAYER:
        return [np.ascontiguousarray(src[s]) for s in
                (np.s_[0::2, 0::2], np.s_[0::2, 1::2], np.s_[1::2, 0::2], np.s_[1::2, 1::2])]
    if mode == RGB:
        R, G, B = src[:, :, 0], src[:, :, 1], src[:, :, 2]
        return [np.ascontiguousarray(G), np.ascontiguousarray(R - G),
                np.ascontiguousarray(B - G)]            # reversible green-subtract
    return [src]                                        # gray


def _merge(planes, mode, H, W):
    """Invert ``_split``: planes -> the original int32 array."""
    if mode == BAYER:
        out = np.zeros((H, W), dtype=np.int32)
        for sl, p in zip((np.s_[0::2, 0::2], np.s_[0::2, 1::2],
                          np.s_[1::2, 0::2], np.s_[1::2, 1::2]), planes):
            out[sl] = p
        return out
    if mode == RGB:
        G, RmG, BmG = planes
        return np.stack([RmG + G, G, BmG + G], axis=-1)
    return planes[0]


def _empty_planes(mode, H, W):
    """Zero int32 planes with the right shapes for decode (to learn plane sizes)."""
    if mode == BAYER:
        z = np.zeros((H, W), dtype=np.int32)
        return [np.ascontiguousarray(z[s]) for s in
                (np.s_[0::2, 0::2], np.s_[0::2, 1::2], np.s_[1::2, 0::2], np.s_[1::2, 1::2])]
    if mode == RGB:
        return [np.zeros((H, W), dtype=np.int32) for _ in range(3)]
    return [np.zeros((H, W), dtype=np.int32)]


def encode(img, bayer=True):
    """Encode an image. A 3D HxWx3 array is treated as RGB; a 2D array as a Bayer
    mosaic (``bayer=True``, default) or a single gray plane (``bayer=False``)."""
    img = np.ascontiguousarray(img)
    itemsize = img.dtype.itemsize
    if img.ndim == 3:
        if img.shape[2] != 3:
            raise ValueError("RGB image must be HxWx3")
        mode = RGB
        H, W = img.shape[:2]
    elif img.ndim == 2:
        mode = BAYER if bayer else GRAY
        H, W = img.shape
    else:
        raise ValueError("image must be 2D (gray/Bayer) or 3D HxWx3 (RGB)")

    scale = _scale(itemsize)
    parts = []                                  # (selector, ctxcoder blob) per plane
    for p in _split(img.astype(np.int32), mode):
        best = None
        for code, kind in _PREDICTORS:
            blob = ctxcoder.encode(predictors.forward(p, kind, scale).reshape(-1))
            if best is None or len(blob) < len(best[1]):
                best = (code, blob)
        parts.append(best)

    header = bytearray(MAGIC)
    header += bytes([VERSION, mode, itemsize])
    header += int(H).to_bytes(4, "big")
    header += int(W).to_bytes(4, "big")
    header += (zlib.crc32(img.tobytes()) & 0xFFFFFFFF).to_bytes(4, "big")
    header.append(len(parts))
    body = bytearray()
    for code, b in parts:
        body.append(code)
        body += len(b).to_bytes(4, "big")
        body += b
    return bytes(header) + bytes(body)


def decode(blob):
    """Decode a RIMG container back to the original array (byte-exact)."""
    if blob[:4] != MAGIC:
        raise ValueError("not a RIMG container")
    if blob[4] != VERSION:
        raise ValueError(f"unsupported RIMG version {blob[4]}")
    mode = blob[5]
    itemsize = blob[6]
    H = int.from_bytes(blob[7:11], "big")
    W = int.from_bytes(blob[11:15], "big")
    crc = int.from_bytes(blob[15:19], "big")
    n_planes = blob[19]
    pos = 20

    templates = _empty_planes(mode, H, W)
    if len(templates) != n_planes:
        raise ValueError("plane count mismatch")
    scale = _scale(itemsize)
    planes = []
    for tmpl in templates:
        code = blob[pos]
        pos += 1
        n = int.from_bytes(blob[pos:pos + 4], "big")
        pos += 4
        chunk = blob[pos:pos + n]
        pos += n
        res = np.asarray(ctxcoder.decode(chunk, tmpl.size), dtype=np.int32).reshape(tmpl.shape)
        planes.append(predictors.reconstruct(res, _KIND[code], scale))

    arr = _merge(planes, mode, H, W)
    dtype = "<u2" if itemsize == 2 else np.uint8
    img = arr.astype(dtype)
    if (zlib.crc32(np.ascontiguousarray(img).tobytes()) & 0xFFFFFFFF) != crc:
        raise ValueError("checksum mismatch — corrupt data")
    return np.ascontiguousarray(img)
