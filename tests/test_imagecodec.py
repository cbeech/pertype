"""Round-trip tests for the raw-image codec (MED + ctxcoder)."""
import numpy as np

from pertype import imagecodec


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


def test_rle_selected_on_sparse():
    # a sparse plane (mostly one value) must round-trip and pick the RLE coder
    rng = np.random.default_rng(3)
    a = np.zeros((128, 128), dtype=np.uint16)
    a.reshape(-1)[rng.integers(0, 128 * 128, 80)] = rng.integers(1, 4000, 80)
    blob = imagecodec.encode(a, bayer=False)
    assert np.array_equal(imagecodec.decode(blob), a)
    assert blob[20] == imagecodec.RLE                 # first plane's selector
    assert len(blob) < a.nbytes // 8                  # and it's tiny


def test_signed_int16_roundtrip():
    rng = np.random.default_rng(4)
    a = rng.integers(-500, 2000, (64, 80), dtype=np.int16)
    out = imagecodec.decode(imagecodec.encode(a, bayer=False))
    assert np.array_equal(out.view(np.int16), a)      # byte-exact (dtype -> uint16)


def test_volume_roundtrip_and_delta():
    rng = np.random.default_rng(5)
    base = np.cumsum(np.cumsum(rng.standard_normal((48, 48)) * 4, 0), 1)
    vol = np.stack([base + i * 3 + rng.standard_normal((48, 48)) * 2 for i in range(8)])
    vol = (vol - vol.min()).astype(np.uint16)
    enc = imagecodec.encode_volume(vol)
    assert np.array_equal(imagecodec.decode_volume(enc), vol)
    per_slice = sum(len(imagecodec.encode(vol[i], bayer=False)) for i in range(vol.shape[0]))
    assert len(enc) < per_slice                       # inter-slice delta helps


def test_wrong_magic_rejected():
    for bad in (b"XXXX" + b"\x00" * 20,):
        try:
            imagecodec.decode(bad)
        except ValueError:
            continue
        assert False, "expected ValueError on bad magic"
