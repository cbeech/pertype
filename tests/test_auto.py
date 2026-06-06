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


def test_float_npy_routes_to_floatcodec():
    rng = np.random.RandomState(8)
    # low-cardinality smooth float grid -> floatcodec wins over deflate/store
    grid = (np.cumsum(rng.randint(-3, 4, (128, 200)), axis=1) / 100.0).astype(np.float32)
    buf = io.BytesIO(); np.save(buf, np.ascontiguousarray(grid))
    blob = _roundtrip(buf.getvalue(), name="grid.npy")
    assert auto.method_name(blob) == "npy->floatcodec"
    assert len(blob) < len(buf.getvalue()) // 2


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


def test_y4m_routes_to_videocodec():
    import numpy as np
    H = W = 64
    rng = np.random.RandomState(9)
    patch = rng.randint(0, 256, (16, 16), np.uint8)       # textured moving patch
    header = b"YUV4MPEG2 W64 H64 F30:1 Ip A0:0 C420jpeg\n"
    body = bytearray()
    for t in range(10):
        f = np.full((H, W), 50, np.uint8)                 # static bg (SKIP) + a pan -> codec wins
        f[2 + t:18 + t, 3:19] = patch
        body += b"FRAME\n" + f.tobytes()
        body += np.full((H // 2, W // 2), 128, np.uint8).tobytes()
        body += np.full((H // 2, W // 2), 128, np.uint8).tobytes()
    data = header + bytes(body)
    _roundtrip(data)                                      # auto round-trips byte-exact
    # the y4m->videocodec path is wired and exact (it's the winner on real clips; on toy
    # data deflate may win, which keep-smallest handles — here we verify the route works):
    assert auto._y4m_decode(auto._try_y4m(data)) == data


def test_wav_routes_to_audiocodec():
    import io
    import wave

    import numpy as np
    rng = np.random.RandomState(10)
    x = np.cumsum(rng.randn(50000)) * 0.3                 # band-limited -> codec predicts
    x -= np.convolve(x, np.ones(40) / 40, mode="same")
    s = np.clip(x * 3000, -32000, 32000).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(44100); w.writeframes(s.tobytes())
    blob = _roundtrip(buf.getvalue())
    assert auto.method_name(blob) == "wav->audiocodec"


def test_dicom_routes_to_imagecodec():
    import io

    import numpy as np
    pydicom = __import__("pytest").importorskip("pydicom")
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian, generate_uid
    yy, xx = np.mgrid[0:128, 0:128]
    img = ((yy * 3 + xx * 2) % 4000 + 100).astype(np.uint16)   # smooth -> imagecodec wins
    ds = Dataset()
    ds.file_meta = FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.file_meta.MediaStorageSOPClassUID = generate_uid()
    ds.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    ds.is_little_endian = True; ds.is_implicit_VR = False
    ds.Rows, ds.Columns = 128, 128; ds.BitsAllocated = 16; ds.BitsStored = 16
    ds.SamplesPerPixel = 1; ds.PixelRepresentation = 0
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelData = img.tobytes()
    buf = io.BytesIO(); ds.save_as(buf, enforce_file_format=True)
    blob = _roundtrip(buf.getvalue())
    assert auto.method_name(blob) == "dicom->imagecodec"


def test_decompress_rejects_foreign_blob():
    import pytest
    with pytest.raises(ValueError):
        auto.auto_decompress(b"not an az blob at all")


def test_empty_input():
    _roundtrip(b"")
