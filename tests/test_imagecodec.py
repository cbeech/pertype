"""Round-trip tests for the raw-image codec (MED + ctxcoder)."""
import numpy as np

from compressor import imagecodec


def _rt(img, bayer):
    blob = imagecodec.encode(img, bayer=bayer)
    out = imagecodec.decode(blob)
    assert out.dtype == np.uint16
    assert np.array_equal(out, img), (bayer, img.shape)


def test_roundtrip_bayer_and_plane():
    rng = np.random.default_rng(0)
    for bayer in (True, False):
        for shape in [(2, 2), (4, 6), (64, 64), (127, 129), (200, 150)]:
            _rt(rng.integers(0, 16384, shape, dtype=np.uint16), bayer)


def test_roundtrip_rgb():
    rng = np.random.default_rng(1)
    for shape in [(2, 2, 3), (64, 48, 3), (101, 77, 3)]:
        for dt, hi in [(np.uint8, 256), (np.uint16, 16384)]:
            img = rng.integers(0, hi, shape, dtype=dt)
            out = imagecodec.decode(imagecodec.encode(img))
            assert out.dtype == dt and np.array_equal(out, img), (shape, dt)


def test_roundtrip_value_extremes_and_flat():
    _rt(np.zeros((32, 40), dtype=np.uint16), True)
    _rt(np.full((32, 40), 65535, dtype=np.uint16), True)
    _rt(np.full((30, 30), 4000, dtype=np.uint16), False)


def test_roundtrip_smooth_gradient_compresses():
    g = (np.add.outer(np.arange(128), np.arange(128)) * 8).astype(np.uint16)
    blob = imagecodec.encode(g, bayer=False)
    assert np.array_equal(imagecodec.decode(blob), g)
    assert len(blob) < g.nbytes // 2          # a smooth ramp must shrink a lot


def test_wrong_magic_rejected():
    try:
        imagecodec.decode(b"XXXX" + b"\x00" * 20)
    except ValueError:
        return
    assert False, "expected ValueError on bad magic"
