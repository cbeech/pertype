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


def test_cli_y4m_roundtrip(tmp_path=None):
    """CLI video-encode -> video-decode reproduces the .y4m byte-exact."""
    import os
    import tempfile
    from compressor import cli

    H, W, T = 32, 64, 3                          # chroma 16x32 -> multiples of 16
    rng = np.random.default_rng(11)
    header = b"YUV4MPEG2 W64 H32 F30:1 Ip A0:0 C420jpeg\n"
    body = bytearray()
    bg = rng.integers(40, 60, (H, W), dtype=np.uint8)
    for t in range(T):
        f = bg.copy()
        f[2 + t:10 + t, 3 + t:11 + t] = rng.integers(0, 256, (8, 8))     # motion + change
        body += b"FRAME\n" + f.tobytes()
        body += rng.integers(0, 256, (H // 2, W // 2), dtype=np.uint8).tobytes()  # U
        body += rng.integers(0, 256, (H // 2, W // 2), dtype=np.uint8).tobytes()  # V
    src = header + bytes(body)

    d = tempfile.mkdtemp()
    y4m, vid, out = os.path.join(d, "c.y4m"), os.path.join(d, "c.vid"), os.path.join(d, "o.y4m")
    with open(y4m, "wb") as fh:
        fh.write(src)
    cli.main(["video-encode", y4m, "-o", vid])
    cli.main(["video-decode", vid, "-o", out])
    with open(out, "rb") as fh:
        assert fh.read() == src, "CLI .y4m round-trip not byte-exact"


def _cli_roundtrips(src_bytes):
    import os
    import tempfile
    from compressor import cli
    d = tempfile.mkdtemp()
    y, v, o = os.path.join(d, "c.y4m"), os.path.join(d, "c.vid"), os.path.join(d, "o.y4m")
    with open(y, "wb") as fh:
        fh.write(src_bytes)
    cli.main(["video-encode", y, "-o", v])
    cli.main(["video-decode", v, "-o", o])
    with open(o, "rb") as fh:
        return fh.read() == src_bytes


def _build_y4m(ctag, W, H, T, plane_dims, frame_hdr=b"FRAME\n", seed=0):
    rng = np.random.default_rng(seed)
    body = bytearray()
    for _ in range(T):
        body += frame_hdr
        for (ph, pw) in plane_dims:
            body += rng.integers(0, 256, (ph, pw), dtype=np.uint8).tobytes()
    return f"YUV4MPEG2 W{W} H{H} F30:1 {ctag}\n".encode() + bytes(body)


def test_cli_y4m_444():
    src = _build_y4m("C444", 64, 32, 3, [(32, 64), (32, 64), (32, 64)], seed=20)
    assert _cli_roundtrips(src)


def test_cli_y4m_422():
    src = _build_y4m("C422", 64, 32, 3, [(32, 64), (32, 32), (32, 32)], seed=24)
    assert _cli_roundtrips(src)


def test_cli_y4m_mono():
    src = _build_y4m("Cmono", 64, 32, 3, [(32, 64)], seed=21)
    assert _cli_roundtrips(src)


def test_cli_y4m_frame_params():
    # per-frame header carries parameters -> must be preserved verbatim
    src = _build_y4m("C420jpeg", 64, 32, 3, [(32, 64), (16, 32), (16, 32)],
                     frame_hdr=b"FRAME Xfoo\n", seed=22)
    assert _cli_roundtrips(src)


def test_dimension_check():
    bad = np.zeros((2, 30, 30), dtype=np.uint8)               # not multiples of 16
    try:
        vc.encode(bad)
        assert False, "expected ValueError"
    except ValueError:
        pass
