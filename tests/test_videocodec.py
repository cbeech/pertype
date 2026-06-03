"""Round-trip tests for the lossless video codec (encode -> decode == frames)."""
import numpy as np

from compressor import videocodec as vc


def _clip(T=5, H=32, W=48, seed=0):
    """Synthetic clip exercising all three block modes: a static background
    (SKIP), a panning textured patch (INTER), and a noisy region (INTRA).
    Sizes scale to the frame so it also works on small (e.g. chroma) frames."""
    rng = np.random.default_rng(seed)
    bg = rng.integers(40, 60, (H, W)).astype(np.int64)        # near-static background
    ps = min(12, H // 2, W // 2)                              # moving patch size
    patch = rng.integers(0, 256, (ps, ps)).astype(np.int64)
    nh, nw = min(16, H), min(16, W)                           # fresh-noise corner
    frames = []
    for t in range(T):
        f = bg.copy()
        oy, ox = min(1 + t, H - ps), min(1 + t, W - ps)       # ~1 px/frame pan, clamped
        f[oy:oy + ps, ox:ox + ps] = patch
        f[0:nh, W - nw:W] = rng.integers(0, 256, (nh, nw))    # fresh noise -> intra
        frames.append(f)
    return np.stack(frames).astype(np.uint8)


def test_roundtrip_basic():
    frames = _clip()
    assert np.array_equal(vc.decode(vc.encode(frames)), frames)


def test_roundtrip_single_frame():
    frames = _clip(T=1)
    assert np.array_equal(vc.decode(vc.encode(frames)), frames)


def test_roundtrip_fully_static():
    f = np.full((16, 16), 123, dtype=np.uint8)
    frames = np.stack([f, f, f])                              # all SKIP after frame 0
    assert np.array_equal(vc.decode(vc.encode(frames)), frames)


def test_roundtrip_random():
    rng = np.random.default_rng(3)
    frames = rng.integers(0, 256, (4, 32, 32), dtype=np.uint8)
    assert np.array_equal(vc.decode(vc.encode(frames)), frames)


def test_roundtrip_larger():
    frames = _clip(T=6, H=48, W=64, seed=7)
    assert np.array_equal(vc.decode(vc.encode(frames)), frames)


def test_yuv_roundtrip():
    Y = _clip(T=4, H=32, W=48, seed=1)
    U = _clip(T=4, H=16, W=32, seed=2)
    V = _clip(T=4, H=16, W=32, seed=3)
    blob = vc.encode_yuv(Y, U, V)
    out = vc.decode_yuv(blob)
    assert len(out) == 3
    assert np.array_equal(out[0], Y)
    assert np.array_equal(out[1], U)
    assert np.array_equal(out[2], V)


def test_dimension_check():
    bad = np.zeros((2, 30, 30), dtype=np.uint8)               # not multiples of 16
    try:
        vc.encode(bad)
        assert False, "expected ValueError"
    except ValueError:
        pass
