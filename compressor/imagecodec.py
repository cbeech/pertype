"""Lossless raw / photo / scientific image codec: 2D prediction + adaptive coding.

Each plane is coded with whichever of three coders is smallest (a 1-byte selector
per plane), so the codec adapts to the content:

* **MED** — fast JPEG-LS predictor + order-2 ``ctxcoder`` (the cheap baseline).
* **CALIC** — a full integrated codec (GAP prediction + per-context bias correction +
  energy-conditional entropy coding); wins on continuous-tone data (photos, raw,
  medical CT/MR).
* **RLE** — run-length of identical values + ``ctxcoder``; wins on sparse / mask /
  label data (large constant regions) where prediction can't beat an LZ-style run
  pass.

The GAP/CALIC gradient threshold *scale* is chosen per plane from its value range
(a few candidates are tried, the best stored), so 8-bit, 12/14/16-bit, and the
small-range inter-slice deltas of a 3D volume all get tracked thresholds.

Modes: a 2D array is a single **gray** plane (``bayer=False``) or a **Bayer** mosaic
(4 RGGB sub-planes); a 3D HxWx3 array is **RGB** (reversible green-subtract). A stack
of slices is a **volume** (``encode_volume``): slice 0 coded directly, each later
slice as its delta from the previous one (CT/MR/FITS volumes are highly inter-slice
redundant). No LZ dictionary, no trained model. Byte-exact; a CRC guards round-trip.

2D container (big-endian)::

    magic "RIMG"; version u8; mode u8; itemsize u8; height u32; width u32; crc32 u32;
    n_planes u8; then per plane: selector u8, scale u16, length u32, blob.

Volume container "RVOL": version u8; itemsize u8; nslices u16; H u32; W u32; crc32 u32;
then per slice: selector u8, scale u16, length u32, blob (slice 0 raw, rest = delta).
"""
import zlib

import numpy as np

from compressor import ctxcoder, predictors

MAGIC = b"RIMG"
VMAGIC = b"RVOL"
VERSION = 4
GRAY, BAYER, RGB = 0, 1, 2

# Coder selectors. MED/CALIC/RLE are tried by the encoder; GAP/Paeth never win once
# CALIC is present, so they're not tried — but decode honours every value, so any can
# be re-enabled without a format change.
MED, PAETH, GAP, CALIC, RLE = 0, 1, 2, 3, 4
_KIND = {MED: "med", PAETH: "paeth", GAP: "gap", CALIC: "calic"}


def _scales(plane):
    """Candidate GAP/CALIC threshold scales for a plane: 1 (8-bit-like) plus one
    matched to its value range. The thresholds (8/32/80) are tuned for an 8-bit
    gradient spread, so a wider-range plane wants a proportionally larger scale."""
    rng = int(plane.max()) - int(plane.min()) if plane.size else 0
    return sorted({1, max(1, rng // 256)})


# --- run-length coder (the LZ-style pre-pass for sparse / constant-region data) ---
def _rle_encode(plane):
    flat = np.ascontiguousarray(plane).reshape(-1)
    if flat.size == 0:
        return (0).to_bytes(4, "big")
    starts = np.concatenate([[0], np.flatnonzero(np.diff(flat)) + 1])
    lens = np.diff(np.concatenate([starts, [flat.size]]))
    vals = flat[starts]
    vb = ctxcoder.encode(vals.astype(np.int64))
    lb = ctxcoder.encode(lens.astype(np.int64))
    return len(starts).to_bytes(4, "big") + len(vb).to_bytes(4, "big") + vb + lb


def _rle_decode(blob, H, W):
    nruns = int.from_bytes(blob[0:4], "big")
    if nruns == 0:
        return np.zeros((H, W), dtype=np.int32)
    vlen = int.from_bytes(blob[4:8], "big")
    vals = np.asarray(ctxcoder.decode(blob[8:8 + vlen], nruns), dtype=np.int32)
    lens = np.asarray(ctxcoder.decode(blob[8 + vlen:], nruns), dtype=np.int64)
    return np.repeat(vals, lens).astype(np.int32).reshape(H, W)


def _code_plane(plane):
    """Pick the cheapest coder (and scale) for one int32 plane. Returns
    ``(selector, scale, blob)``."""
    best = (MED, 1, ctxcoder.encode(predictors.forward(plane, "med", 1).reshape(-1)))
    for sc in _scales(plane):
        blob = predictors.calic_full_encode(plane, sc)
        if len(blob) < len(best[2]):
            best = (CALIC, sc, blob)
    rb = _rle_encode(plane)
    if len(rb) < len(best[2]):
        best = (RLE, 1, rb)
    return best


def _decode_plane(code, scale, blob, H, W):
    if code == CALIC:
        return predictors.calic_full_decode(blob, H, W, scale)
    if code == RLE:
        return _rle_decode(blob, H, W)
    res = np.asarray(ctxcoder.decode(blob, H * W), dtype=np.int32).reshape(H, W)
    return predictors.reconstruct(res, _KIND[code], scale)


def _pack_plane(code, scale, blob):
    return bytes([code]) + int(scale).to_bytes(2, "big") + len(blob).to_bytes(4, "big") + blob


def _read_plane_hdr(blob, pos):
    code = blob[pos]
    scale = int.from_bytes(blob[pos + 1:pos + 3], "big")
    n = int.from_bytes(blob[pos + 3:pos + 7], "big")
    chunk = blob[pos + 7:pos + 7 + n]
    return code, scale, chunk, pos + 7 + n


# --- 2D images: gray / Bayer / RGB ------------------------------------------------
def _split(src, mode):
    if mode == BAYER:
        return [np.ascontiguousarray(src[s]) for s in
                (np.s_[0::2, 0::2], np.s_[0::2, 1::2], np.s_[1::2, 0::2], np.s_[1::2, 1::2])]
    if mode == RGB:
        R, G, B = src[:, :, 0], src[:, :, 1], src[:, :, 2]
        return [np.ascontiguousarray(G), np.ascontiguousarray(R - G), np.ascontiguousarray(B - G)]
    return [src]


def _plane_shapes(mode, H, W):
    if mode == BAYER:
        z = np.zeros((H, W), dtype=np.int32)
        return [z[s].shape for s in
                (np.s_[0::2, 0::2], np.s_[0::2, 1::2], np.s_[1::2, 0::2], np.s_[1::2, 1::2])]
    if mode == RGB:
        return [(H, W)] * 3
    return [(H, W)]


def _merge(planes, mode, H, W):
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


def encode(img, bayer=True):
    """Encode an image. 3D HxWx3 -> RGB; 2D -> Bayer mosaic (default) or gray plane."""
    img = np.ascontiguousarray(img)
    itemsize = img.dtype.itemsize
    if img.ndim == 3:
        if img.shape[2] != 3:
            raise ValueError("RGB image must be HxWx3")
        mode, (H, W) = RGB, img.shape[:2]
    elif img.ndim == 2:
        mode, (H, W) = (BAYER if bayer else GRAY), img.shape
    else:
        raise ValueError("image must be 2D (gray/Bayer) or 3D HxWx3 (RGB)")

    parts = [_code_plane(p) for p in _split(img.astype(np.int32), mode)]
    header = bytearray(MAGIC)
    header += bytes([VERSION, mode, itemsize])
    header += int(H).to_bytes(4, "big") + int(W).to_bytes(4, "big")
    header += (zlib.crc32(img.tobytes()) & 0xFFFFFFFF).to_bytes(4, "big")
    header.append(len(parts))
    body = b"".join(_pack_plane(*p) for p in parts)
    return bytes(header) + bytes(body)


def decode(blob):
    """Decode a RIMG container back to the original array (byte-exact)."""
    if blob[:4] != MAGIC:
        raise ValueError("not a RIMG container")
    if blob[4] != VERSION:
        raise ValueError(f"unsupported RIMG version {blob[4]}")
    mode, itemsize = blob[5], blob[6]
    H = int.from_bytes(blob[7:11], "big")
    W = int.from_bytes(blob[11:15], "big")
    crc = int.from_bytes(blob[15:19], "big")
    n_planes = blob[19]
    shapes = _plane_shapes(mode, H, W)
    if len(shapes) != n_planes:
        raise ValueError("plane count mismatch")
    pos, planes = 20, []
    for ph, pw in shapes:
        code, scale, chunk, pos = _read_plane_hdr(blob, pos)
        planes.append(_decode_plane(code, scale, chunk, ph, pw))

    dtype = "<u2" if itemsize == 2 else np.uint8
    img = np.ascontiguousarray(_merge(planes, mode, H, W).astype(dtype))
    if (zlib.crc32(img.tobytes()) & 0xFFFFFFFF) != crc:
        raise ValueError("checksum mismatch — corrupt data")
    return img


# --- 3D volumes: inter-slice delta ------------------------------------------------
def encode_volume(vol):
    """Encode a stack of slices ``(nslices, H, W)``. Slice 0 is coded directly; each
    later slice is coded as its delta from the previous slice — adjacent CT/MR/FITS
    slices are highly redundant, so the deltas are small and code far cheaper."""
    vol = np.ascontiguousarray(vol)
    if vol.ndim != 3:
        raise ValueError("volume must be 3D (nslices, H, W)")
    N, H, W = vol.shape
    itemsize = vol.dtype.itemsize
    v = vol.astype(np.int32)
    parts = []
    prev = None
    for i in range(N):
        plane = v[i] if prev is None else v[i] - prev
        parts.append(_code_plane(np.ascontiguousarray(plane)))
        prev = v[i]
    header = bytearray(VMAGIC)
    header += bytes([VERSION, itemsize])
    header += int(N).to_bytes(2, "big") + int(H).to_bytes(4, "big") + int(W).to_bytes(4, "big")
    header += (zlib.crc32(vol.tobytes()) & 0xFFFFFFFF).to_bytes(4, "big")
    body = b"".join(_pack_plane(*p) for p in parts)
    return bytes(header) + bytes(body)


def decode_volume(blob):
    """Decode an ``encode_volume`` container back to the ``(nslices, H, W)`` stack."""
    if blob[:4] != VMAGIC:
        raise ValueError("not a RVOL container")
    if blob[4] != VERSION:
        raise ValueError(f"unsupported RVOL version {blob[4]}")
    itemsize = blob[5]
    N = int.from_bytes(blob[6:8], "big")
    H = int.from_bytes(blob[8:12], "big")
    W = int.from_bytes(blob[12:16], "big")
    crc = int.from_bytes(blob[16:20], "big")
    pos = 20
    out = np.zeros((N, H, W), dtype=np.int32)
    prev = None
    for i in range(N):
        code, scale, chunk, pos = _read_plane_hdr(blob, pos)
        plane = _decode_plane(code, scale, chunk, H, W)
        out[i] = plane if prev is None else plane + prev
        prev = out[i]
    dtype = "<u2" if itemsize == 2 else np.uint8
    vol = np.ascontiguousarray(out.astype(dtype))
    if (zlib.crc32(vol.tobytes()) & 0xFFFFFFFF) != crc:
        raise ValueError("checksum mismatch — corrupt data")
    return vol
