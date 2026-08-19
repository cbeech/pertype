"""CCSDS comparison: libpfc vs a CCSDS-121-class baseline (MED predictor + block-adaptive Rice)
and JPEG-LS (the LOCO-I standard), on real 16-bit instrument imagery.

CCSDS-121 is the flight lossless-entropy standard (block-adaptive Rice on a prediction residual);
this baseline pairs it with the same MED predictor pfc uses, so the comparison isolates the entropy
stage: range coder + adaptive context (pfc) vs Rice (CCSDS-121). JPEG-LS is the gradient-context bar.

Build first:  make sharedlib
Run:          PYTHONPATH=<repo> python3 test/ccsds_compare.py [image.tif ...]
"""
import ctypes as C
import glob
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
from bench_real import load_lib, pfc_image, load_planes  # noqa: E402


def med_residuals(img):
    """Full-image MED (LOCO-I) residuals; edges predicted from available causal neighbour."""
    x = img.astype(np.int64)
    h, w = x.shape
    left = np.zeros_like(x); left[:, 1:] = x[:, :-1]
    up = np.zeros_like(x); up[1:, :] = x[:-1, :]
    ul = np.zeros_like(x); ul[1:, 1:] = x[:-1, :-1]
    lo = np.minimum(left, up); hi = np.maximum(left, up)
    pred = np.where(ul >= hi, lo, np.where(ul <= lo, hi, left + up - ul))
    pred[0, :] = left[0, :]          # first row: left
    pred[:, 0] = up[:, 0]            # first col: up
    pred[0, 0] = 1 << (img.dtype.itemsize * 8 - 1)
    return x - pred


def rice_bits(resid, J=32):
    """Block-adaptive Rice (CCSDS-121-class) bit count on zigzag-mapped residuals."""
    u = ((resid << 1) ^ (resid >> 63)).astype(np.uint64).ravel()  # zigzag (int64)
    n = u.size
    total = 0
    for b0 in range(0, n, J):
        blk = u[b0:b0 + J]
        m = blk.size
        best = m * 17                       # raw 17-bit fallback per sample
        for k in range(0, 18):
            bits = int(np.sum(blk >> np.uint64(k))) + m * (k + 1)
            if bits < best:
                best = bits
        total += best + 5                   # ~5 bits to signal the option/k per block
    return (total + 7) // 8


def main():
    lib = load_lib()
    try:
        import imagecodecs as ic
        have_ic = True
    except Exception:
        have_ic = False

    args = sys.argv[1:]
    if not args:
        pool = "/tmp/claude-1000/-home-craig-Dev-compression/d3fc84dc-5a79-47c9-8f87-528364411598/scratchpad/spatialomics"
        args = sorted(glob.glob(os.path.join(pool, "*.tif*")))[:1]

    traw = tpfc = trice = tjls = 0
    for path in args:
        for k, plane in enumerate(load_planes(path)):
            plane = np.ascontiguousarray(plane).astype("<u2")
            raw = plane.nbytes
            pfc_sz, ok = pfc_image(lib, plane)
            assert ok, "pfc not lossless!"
            rice = rice_bits(med_residuals(plane))
            jls = len(ic.jpegls_encode(plane)) if have_ic else 0
            traw += raw; tpfc += pfc_sz; trice += rice; tjls += jls
            print(f"  {os.path.basename(path)}[{k}] {plane.shape}  "
                  f"pfc {raw/pfc_sz:5.2f}x  CCSDS-121-Rice {raw/rice:5.2f}x"
                  + (f"  JPEG-LS {raw/jls:5.2f}x" if jls else "")
                  + f"   (pfc vs CCSDS-121 {(1-pfc_sz/rice)*100:+.1f}%)")

    print(f"\n  TOTAL  pfc {traw/tpfc:.2f}x   CCSDS-121-Rice {traw/trice:.2f}x"
          + (f"   JPEG-LS {traw/tjls:.2f}x" if tjls else "")
          + f"   |  pfc beats CCSDS-121 by {(1-tpfc/trice)*100:+.1f}%")


if __name__ == "__main__":
    main()
