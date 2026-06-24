"""Host bridge: run libpfc (the flight C core) on REAL instrument-class 16-bit imagery via ctypes,
verify lossless round-trip, and compare its ratio to the CCSDS-123-class bar (JPEG-LS) and JPEG-XL.

Build the shared lib first:  make sharedlib
Run:  python3 test/bench_real.py <image.tif|.npy> [more...]
With no args it falls back to any 16-bit TIFFs found in the scratch spatial-omics pool.
"""
import ctypes as C
import glob
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(HERE, "..", "build", "libpfc.so")

PFC_CODEC_IMAGE = 1


class Params(C.Structure):
    _fields_ = [("width", C.c_uint32), ("height", C.c_uint32), ("count", C.c_uint32),
                ("bitdepth", C.c_uint8), ("elem", C.c_uint8), ("is_signed", C.c_uint8)]


def load_lib():
    lib = C.CDLL(LIB)
    lib.pfc_workmem_bytes.restype = C.c_size_t
    lib.pfc_bound.restype = C.c_size_t
    lib.pfc_bound.argtypes = [C.c_int, C.c_size_t]
    for fn in ("pfc_encode", "pfc_decode"):
        getattr(lib, fn).restype = C.c_int
    lib.pfc_encode.argtypes = [C.c_int, C.POINTER(Params), C.c_void_p, C.c_size_t,
                               C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t), C.c_void_p]
    lib.pfc_decode.argtypes = [C.c_void_p, C.c_size_t, C.c_void_p, C.c_size_t,
                               C.POINTER(C.c_size_t), C.c_void_p]
    return lib


def pfc_image(lib, plane):
    """Encode+decode a 2-D uint16 plane through libpfc; return (compressed_bytes, lossless?)."""
    h, w = plane.shape
    src = np.ascontiguousarray(plane, dtype="<u2")
    n_in = src.nbytes
    cap = lib.pfc_bound(PFC_CODEC_IMAGE, n_in)
    enc = (C.c_uint8 * cap)()
    work = (C.c_uint8 * lib.pfc_workmem_bytes())()
    p = Params(w, h, 0, 16, 0, 0)
    out = C.c_size_t(0)
    st = lib.pfc_encode(PFC_CODEC_IMAGE, C.byref(p), src.ctypes.data, n_in,
                        enc, cap, C.byref(out), work)
    assert st == 0, f"encode status {st}"
    dec = np.empty(h * w, dtype="<u2")
    dout = C.c_size_t(0)
    st = lib.pfc_decode(enc, out.value, dec.ctypes.data, n_in, C.byref(dout), work)
    assert st == 0, f"decode status {st}"
    ok = np.array_equal(dec.reshape(h, w), src)
    return out.value, ok


def load_planes(path):
    if path.endswith(".npy"):
        a = np.load(path)
    else:
        import tifffile
        a = tifffile.imread(path)
    a = np.asarray(a)
    if a.ndim == 2:
        return [a]
    if a.ndim == 3:                     # (C,H,W) channel stack
        return [a[i] for i in range(a.shape[0])]
    raise ValueError(f"unhandled shape {a.shape}")


def main():
    lib = load_lib()
    print(f"libpfc workmem = {lib.pfc_workmem_bytes()} bytes")
    args = sys.argv[1:]
    if not args:
        pool = "/tmp/claude-1000/-home-craig-Dev-compression/d3fc84dc-5a79-47c9-8f87-528364411598/scratchpad/spatialomics"
        args = sorted(glob.glob(os.path.join(pool, "*.tif*")))[:1]
        if not args:
            print("no inputs; pass a 16-bit TIFF/npy path"); return

    try:
        import imagecodecs as ic
        have_ic = True
    except Exception:
        have_ic = False

    tot_raw = tot_pfc = tot_jls = tot_jxl = 0
    nlossless = nplanes = 0
    for path in args:
        for k, plane in enumerate(load_planes(path)):
            plane = np.ascontiguousarray(plane).astype("<u2")
            raw = plane.nbytes
            pfc_sz, ok = pfc_image(lib, plane)
            nplanes += 1
            nlossless += int(ok)
            tot_raw += raw; tot_pfc += pfc_sz
            line = f"  {os.path.basename(path)}[{k}] {plane.shape} raw {raw/1e6:.2f}MB  pfc {raw/pfc_sz:5.2f}x  lossless={ok}"
            if have_ic:
                jls = len(ic.jpegls_encode(plane))
                jxl = len(ic.jpegxl_encode(plane, lossless=True, effort=7))
                tot_jls += jls; tot_jxl += jxl
                line += f"  | JPEG-LS {raw/jls:5.2f}x  JPEG-XL {raw/jxl:5.2f}x  (pfc vs JPEG-LS {(1-pfc_sz/jls)*100:+.1f}%)"
            print(line)

    print(f"\n  TOTAL  pfc {tot_raw/tot_pfc:.2f}x   lossless {nlossless}/{nplanes}", end="")
    if tot_jls:
        print(f"   JPEG-LS {tot_raw/tot_jls:.2f}x   JPEG-XL {tot_raw/tot_jxl:.2f}x"
              f"   (pfc vs JPEG-LS {(1-tot_pfc/tot_jls)*100:+.1f}%)")
    else:
        print()


if __name__ == "__main__":
    main()
