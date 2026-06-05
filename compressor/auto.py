"""Automatic compress / decompress: detect the data, route to the ideal codec.

The front door over the specialist codecs. ``auto_compress`` identifies the input
(:mod:`compressor.detect`), builds a few candidate encodings — the matching
specialist plus universal fallbacks — **verifies each round-trips byte-exact**, and
keeps the smallest that verifies. The method is tagged in a 4-byte header so
``auto_decompress`` routes back. Because *store* always verifies, the result is never
worse than the original and never wrong.

What routes to a specialist today (byte-exact, including the format's non-array
metadata): **FITS** int16 images and **.npy** 2D/3D integer arrays -> the image codec
(gray / RGB / inter-slice-delta volume); **text** -> the CSV/delimited-table columnar
codec (:mod:`compressor.csvcolumnar`, which itself falls back to deflate when the data
isn't a regular grid); **opaque binary** -> the fixed-width-record columnar codec
(:mod:`compressor.columnar`, auto-detecting the record period — wins on LiDAR-style point
data, stores otherwise). Everything else falls back to generic deflate or store. The
trained text codec is model-based, so without a shipped model ``auto`` can't get its
trained-dictionary win on arbitrary prose; that's the honest boundary, reported by
``identify``.
"""
import io
import zlib

import numpy as np

from compressor import columnar, csvcolumnar, imagecodec
from compressor.detect import identify

AMAGIC = b"AZ"
AVERSION = 1
M_STORE, M_ZLIB, M_NPY, M_FITS, M_CSV, M_COL = 0, 1, 2, 3, 4, 5


def _wrap(method, payload):
    return AMAGIC + bytes([AVERSION, method]) + bytes(payload)


# --- .npy 2D/3D integer arrays -> imagecodec --------------------------------------
def _img_encode(arr):
    """Pick the image-codec path for an array, or None if it doesn't apply."""
    if arr.dtype.kind not in "iu" or arr.dtype.itemsize not in (1, 2):
        return None
    if arr.ndim == 2:
        return imagecodec.encode(arr, bayer=False)
    if arr.ndim == 3 and arr.shape[2] == 3 and arr.dtype == np.uint8:
        return imagecodec.encode(arr)
    if arr.ndim == 3:
        return imagecodec.encode_volume(arr)
    return None


def _img_decode(blob):
    return imagecodec.decode_volume(blob) if blob[:4] == imagecodec.VMAGIC else imagecodec.decode(blob)


def _try_npy(data):
    try:
        arr = np.load(io.BytesIO(data), allow_pickle=False)
    except Exception:
        return None
    body = data[len(data) - arr.nbytes:]          # np.save: header then C-order data
    header = data[:len(data) - arr.nbytes]
    blob = _img_encode(np.ascontiguousarray(arr))
    if blob is None:
        return None
    return len(header).to_bytes(4, "big") + header + blob


def _npy_decode(payload):
    # The stored npy header (verbatim) carries dtype/shape; the decoded image array's
    # bytes equal the original C-order data (int16 vs uint16 differ only in dtype, not
    # bytes), so body is just its tobytes(). The verify pass guards any mismatch.
    hlen = int.from_bytes(payload[:4], "big")
    header = payload[4:4 + hlen]
    dec = _img_decode(payload[4 + hlen:])
    return header + np.ascontiguousarray(dec).tobytes()


# --- FITS int16 image -> imagecodec (header blocks preserved verbatim) ------------
def _fits_header(data):
    """Return (header_bytes, BITPIX, dims) for the primary HDU, or None."""
    if data[:9] != b"SIMPLE  =":
        return None
    off, hdr = 0, {}
    while off + 2880 <= len(data):
        blk = data[off:off + 2880]; off += 2880; end = False
        for i in range(0, 2880, 80):
            c = blk[i:i + 80].decode("ascii", "replace"); k = c[:8].strip()
            if k == "END":
                end = True; break
            if "=" in c:
                hdr[k] = c[9:].split("/")[0].strip()
        if end:
            break
    else:
        return None
    na = int(hdr.get("NAXIS", 0))
    dims = [int(hdr[f"NAXIS{i}"]) for i in range(1, na + 1)]
    return data[:off], int(hdr.get("BITPIX", 0)), dims


def _try_fits(data):
    info = _fits_header(data)
    if info is None:
        return None
    header, bitpix, dims = info
    if bitpix != 16 or len(dims) != 2:
        return None
    n = dims[0] * dims[1]
    start = len(header)
    arr = np.frombuffer(data[start:start + n * 2], ">i2").reshape(dims[::-1])
    arr = np.ascontiguousarray(arr.astype(np.int16))         # native order
    blob = imagecodec.encode(arr, bayer=False)
    trailing = data[start + n * 2:]                          # padding to 2880
    return (len(header).to_bytes(4, "big") + header
            + len(trailing).to_bytes(4, "big") + trailing + blob)


def _fits_decode(payload):
    hlen = int.from_bytes(payload[:4], "big")
    header = payload[4:4 + hlen]
    tlen = int.from_bytes(payload[4 + hlen:8 + hlen], "big")
    pos = 8 + hlen
    trailing = payload[pos:pos + tlen]
    arr = _img_decode(payload[pos + tlen:])
    body = np.ascontiguousarray(arr).view(np.int16).astype(">i2").tobytes()
    return header + body + trailing


_DECODERS = {M_STORE: lambda p: p, M_ZLIB: zlib.decompress,
             M_NPY: _npy_decode, M_FITS: _fits_decode,
             M_CSV: csvcolumnar.decode, M_COL: columnar.decode}


def auto_compress(data, name=None):
    """Compress ``data`` with the best verified method; returns a tagged blob."""
    det = identify(data, name)
    candidates = [(M_STORE, bytes(data))]
    if det.codec != "store":                       # already-compressed: don't bother
        candidates.append((M_ZLIB, zlib.compress(data, 9)))
    if det.codec == "imagecodec":
        for method, builder in ((M_NPY, _try_npy), (M_FITS, _try_fits)):
            payload = builder(data)
            if payload is not None:
                candidates.append((method, payload))
    if det.kind.startswith("text"):                # CSV/TSV tables -> columnar transpose
        candidates.append((M_CSV, csvcolumnar.encode(data)))
    elif det.codec == "generic":                   # opaque binary -> try record columns
        candidates.append((M_COL, columnar.encode(data)))

    best = None
    for method, payload in candidates:
        blob = _wrap(method, payload)
        try:
            if auto_decompress(blob) == data:      # only keep verified
                if best is None or len(blob) < len(best):
                    best = blob
        except Exception:
            continue
    return best                                    # store always verifies


def auto_decompress(blob):
    """Reconstruct the original bytes from an :func:`auto_compress` blob."""
    if blob[:2] != AMAGIC or blob[2] != AVERSION:
        raise ValueError("not an AZ auto container")
    return _DECODERS[blob[3]](blob[4:])


def method_name(blob):
    """Human label of the method used (for reporting)."""
    return {M_STORE: "store", M_ZLIB: "deflate", M_NPY: "npy->imagecodec",
            M_FITS: "fits->imagecodec", M_CSV: "csv->columnar",
            M_COL: "binary->columnar"}.get(blob[3], "?")
