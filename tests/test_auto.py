"""Tests for the auto compress/decompress front door (detect -> route -> verify)."""
import io
import lzma
import zlib

import numpy as np

from compressor import auto


def _roundtrip(data, name=None):
    blob = auto.auto_compress(data, name=name)
    assert auto.auto_decompress(blob) == data        # byte-exact guarantee
    return blob


def test_store_on_incompressible():
    data = lzma.compress(b"already squeezed " * 100)  # high-entropy stream
    blob = _roundtrip(data)
    assert auto.method_name(blob) == "store"


def test_deflate_on_text():
    data = (b"The quick brown fox jumps over the lazy dog. " * 40
            + b"Lorem ipsum dolor sit amet, consectetur. " * 40)   # prose, not a grid
    blob = _roundtrip(data, name="x.txt")
    assert auto.method_name(blob) in ("deflate", "csv->columnar")   # compressed, not stored
    assert len(blob) < len(data)


def test_csv_routes_to_columnar():
    rows = ["t;v;n"]
    v = 1000
    for i in range(2000):
        v += (i * 7 % 11) - 5
        rows.append(f"2024-01-01;{v/100:.2f};{i}")
    data = ("\n".join(rows) + "\n").encode()
    blob = _roundtrip(data, name="series.csv")
    assert auto.method_name(blob) == "csv->columnar"
    assert len(blob) < len(data) // 3                  # numeric columns crush


def test_binary_records_route_to_columnar():
    rng = np.random.RandomState(7)
    n = 4000
    X = np.cumsum(rng.randint(-3, 4, n)).astype("<i4")
    Y = np.cumsum(rng.randint(-2, 3, n)).astype("<i4")
    rec = np.empty((n, 8), np.uint8)
    rec[:, 0:4] = X.view(np.uint8).reshape(n, 4)
    rec[:, 4:8] = Y.view(np.uint8).reshape(n, 4)
    blob = _roundtrip(rec.tobytes())                   # opaque binary records
    assert auto.method_name(blob) == "binary->columnar"
    assert len(blob) < len(rec.tobytes())


def test_npy_2d_int16_routes_to_imagecodec():
    rng = np.random.RandomState(0)
    base = np.cumsum(rng.randint(-2, 3, size=(64, 64)), axis=1).astype(np.int16)
    buf = io.BytesIO(); np.save(buf, base)
    blob = _roundtrip(buf.getvalue(), name="plane.npy")
    assert auto.method_name(blob) == "npy->imagecodec"


def test_npy_rgb_uint8_roundtrips():
    rng = np.random.RandomState(1)
    img = rng.randint(0, 256, size=(32, 32, 3)).astype(np.uint8)
    buf = io.BytesIO(); np.save(buf, img)
    _roundtrip(buf.getvalue(), name="rgb.npy")          # random RGB -> store, still exact


def test_npy_volume_routes_to_imagecodec():
    rng = np.random.RandomState(2)
    vol = np.cumsum(rng.randint(-1, 2, size=(5, 32, 32)), axis=0).astype(np.int16)
    buf = io.BytesIO(); np.save(buf, vol)
    blob = _roundtrip(buf.getvalue(), name="vol.npy")
    assert auto.method_name(blob) == "npy->imagecodec"


def test_fits_int16_routes_to_imagecodec():
    # A large smooth astronomy-like frame: a 2D gradient + faint noise. Prediction
    # crushes this; deflate can't, so the fits->imagecodec path wins (on a tiny image
    # the verbatim 2880-byte header would let deflate win — auto picks the smaller).
    h, w = 256, 256
    rng = np.random.RandomState(3)
    yy, xx = np.mgrid[0:h, 0:w]
    img = ((yy * 3 + xx * 2) + rng.randint(-2, 3, size=(h, w))).astype(">i2")
    cards = [b"SIMPLE  =                    T",
             b"BITPIX  =                   16",
             b"NAXIS   =                    2",
             ("NAXIS1  = %20d" % w).encode(),
             ("NAXIS2  = %20d" % h).encode(),
             b"END"]
    block = b"".join(c.ljust(80) for c in cards)
    block += b" " * (2880 - len(block) % 2880)
    body = img.tobytes()
    body += b"\x00" * ((2880 - len(body) % 2880) % 2880)
    data = block + body
    blob = _roundtrip(data, name="image.fits")
    assert auto.method_name(blob) == "fits->imagecodec"


def test_decompress_rejects_foreign_blob():
    import pytest
    with pytest.raises(ValueError):
        auto.auto_decompress(b"not an az blob at all")


def test_empty_input():
    _roundtrip(b"")
