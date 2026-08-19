"""R7 cross-check: the flight C ENCODER and the independent pure-Python ground DECODER agree
byte-for-byte, across all four codecs and on real instrument imagery.

Build first:  make sharedlib
Run:          PYTHONPATH=<repo> python3 test/test_crosscheck.py
"""
import ctypes as C
import glob
import os
import struct
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "ground"))
import pfc_decode  # noqa: E402

LIB = os.path.join(os.path.dirname(__file__), "..", "build", "libpfc.so")
IMAGE, SEQ, FLOAT, COLUMNAR = 1, 2, 3, 4


class Params(C.Structure):
    _fields_ = [("width", C.c_uint32), ("height", C.c_uint32), ("count", C.c_uint32),
                ("bitdepth", C.c_uint8), ("elem", C.c_uint8), ("is_signed", C.c_uint8)]


def lib():
    h = C.CDLL(LIB)
    h.pfc_workmem_bytes.restype = C.c_size_t
    h.pfc_bound.restype = C.c_size_t
    h.pfc_bound.argtypes = [C.c_int, C.c_size_t]
    h.pfc_encode.restype = C.c_int
    h.pfc_encode.argtypes = [C.c_int, C.POINTER(Params), C.c_void_p, C.c_size_t,
                             C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t), C.c_void_p]
    return h


def c_encode(h, codec, params, src):
    n = len(src)
    cap = h.pfc_bound(codec, n)
    enc = (C.c_uint8 * cap)()
    work = (C.c_uint8 * h.pfc_workmem_bytes())()
    out = C.c_size_t(0)
    st = h.pfc_encode(codec, C.byref(params), src, n, enc, cap, C.byref(out), work)
    assert st == 0, f"C encode status {st}"
    return bytes(enc[:out.value])


def main():
    h = lib()
    npass = nfail = 0

    def check(name, codec, params, src):
        nonlocal npass, nfail
        stream = c_encode(h, codec, params, src)
        got = pfc_decode.decode(stream)
        ok = (got == src)
        npass += int(ok); nfail += int(not ok)
        print(f"  {'OK ' if ok else 'FAIL'} {name:22} {len(src):>8} B -> {len(stream):>8} B"
              f"  python-decode == original: {ok}")

    # image 16-bit gradient
    w, h_, bd = 192, 160, 16
    img = np.fromfunction(lambda y, x: ((x * 3 + y * 7) & 0xFFFF), (h_, w), dtype=np.int64).astype("<u2")
    check("image16-gradient", IMAGE, Params(w, h_, 0, bd, 0, 0), img.tobytes())

    # image 8-bit
    img8 = (np.fromfunction(lambda y, x: ((x + y) & 0xFF), (120, 200), dtype=np.int64)).astype(np.uint8)
    check("image8-gradient", IMAGE, Params(200, 120, 0, 8, 0, 0), img8.tobytes())

    # flat image with objects + spikes -> exercises run mode + interruptions (R7 over runs)
    flat = np.full((240, 300), 1000, np.uint16)
    flat[60:120, 5:295] = 5000
    rng = np.random.default_rng(7)
    for _ in range(360):
        flat[rng.integers(0, 240), rng.integers(0, 300)] = rng.integers(0, 65536)
    check("image16-flat-runs", IMAGE, Params(300, 240, 0, 16, 0, 0), flat.tobytes())

    # seq int16 ramp
    n = 9000
    seq = ((np.arange(n) % 600) - 300).astype("<i2")
    check("seq-int16", SEQ, Params(0, 0, n, 0, 2, 1), seq.tobytes())

    # float32 smooth
    fl = ((np.arange(6000) % 500) * 0.25).astype("<f4")
    check("float32", FLOAT, Params(0, 0, fl.size, 0, 4, 0), fl.tobytes())

    # columnar records (u32 counter + u16 flag)
    nr, rw = 7000, 6
    rec = np.zeros((nr, rw), np.uint8)
    ctr = (np.arange(nr) * 4 + 1000).astype("<u4")
    rec[:, 0:4] = ctr.view(np.uint8).reshape(nr, 4)
    rec[:, 4] = (np.arange(nr) % 3).astype(np.uint8)
    check("columnar", COLUMNAR, Params(rw, 0, nr, 0, 0, 0), rec.tobytes())

    # spectral cube (BSQ): synthetic spectrally-correlated + real AVIRIS slice if present
    Z, H, W = 24, 60, 50
    cube = np.zeros((Z, H, W), np.uint16)
    base = np.fromfunction(lambda y, x: ((x * 5 + y * 3) & 0xFFF), (H, W), dtype=np.int64)
    for z in range(Z):
        cube[z] = (base + z * 40 + ((np.arange(H)[:, None] + np.arange(W)[None, :] + z) & 7)).astype(np.uint16)
    check("spectral-cube16", 5, Params(W, H, Z, 16, 0, 0), cube.tobytes())
    # Same cube with inter-band refresh bands enabled (elem = refresh interval). Without this case
    # R7 would only ever prove the independent decoder on the refresh=0 path, leaving the whole
    # containment feature unverified against an independent implementation.
    check("spectral-refresh4", 5, Params(W, H, Z, 16, 4, 0), cube.tobytes())
    try:
        import scipy.io as sio
        mat = os.path.expanduser("~/sci_data/hyperspectral/Indian_pines_corrected.mat")
        a = np.asarray(sio.loadmat(mat)["indian_pines_corrected"]).transpose(2, 0, 1).astype("<u2")
        a = np.ascontiguousarray(a[:20])     # first 20 bands, full 145x145
        zz, hh, ww = a.shape
        check("spectral-aviris", 5, Params(ww, hh, zz, 16, 0, 0), a.tobytes())
    except Exception as e:
        print(f"  (skip real AVIRIS spectral cross-check: {e})")

    # real CyCIF 16-bit (if present)
    pool = "/tmp/claude-1000/-home-craig-Dev-compression/d3fc84dc-5a79-47c9-8f87-528364411598/scratchpad/spatialomics"
    tifs = sorted(glob.glob(os.path.join(pool, "*.tif*")))
    if tifs:
        import tifffile
        a = np.asarray(tifffile.imread(tifs[0]))
        planes = [a[i] for i in range(a.shape[0])] if a.ndim == 3 else [a]
        for k, pl in enumerate(planes[:2]):
            pl = np.ascontiguousarray(pl).astype("<u2")
            hh, ww = pl.shape
            check(f"real-cycif[{k}]", IMAGE, Params(ww, hh, 0, 16, 0, 0), pl.tobytes())

    print(f"\n{npass} passed, {nfail} failed")
    sys.exit(1 if nfail else 0)


if __name__ == "__main__":
    main()
