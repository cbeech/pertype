"""CCSDS-123 comparison: does the per-band pfc image codec compete with the hyperspectral flight
standard, whose whole advantage is SPECTRAL prediction (predict a band from co-located samples in
previous bands)?  Run on real AVIRIS Indian Pines (145x145x200, contiguous narrow bands).

We compare predictor quality with the entropy coder held constant (block-adaptive Rice, the CCSDS
entropy class), then show pfc's real arithmetic-coded per-band size:
  - per-band MED (spatial only)            -- what pfc / JPEG-LS predictors can see
  - inter-band delta (spectral-lite)        -- the cheapest spectral predictor
  - CCSDS-123-class (spectral+spatial LS)   -- batch rendition of the standard's adaptive predictor
  - pfc per-band (real)  and  JPEG-LS per-band (real)

Data: AVIRIS Indian Pines (public; EHU GIC hyperspectral scenes repository), expected at
~/sci_data/hyperspectral/Indian_pines_corrected.mat. Needs scipy + (optionally) imagecodecs.

Build first:  make sharedlib
Run:          PYTHONPATH=<repo> python3 test/ccsds123_compare.py
"""
import os
import sys

import numpy as np
import scipy.io as sio

sys.path.insert(0, os.path.dirname(__file__))
from bench_real import load_lib, pfc_image            # noqa: E402
from ccsds_compare import rice_bits, med_residuals    # noqa: E402

MAT = os.path.expanduser("~/sci_data/hyperspectral/Indian_pines_corrected.mat")


def med_resid_cube(cube):
    """Per-band MED residuals (spatial only)."""
    return np.concatenate([med_residuals(cube[z]).ravel() for z in range(cube.shape[0])])


def interband_resid(cube):
    """Spectral-lite: band 0 spatial MED, band z>0 = s(z) - s(z-1)."""
    out = [med_residuals(cube[0]).ravel()]
    for z in range(1, cube.shape[0]):
        out.append((cube[z].astype(np.int64) - cube[z - 1].astype(np.int64)).ravel())
    return np.concatenate(out)


def ccsds123_resid(cube):
    """CCSDS-123-class: per-band least-squares predictor over [prev-band, N, W, prev2-band, 1].
    Batch (per-band LS) rendition of the standard's adaptive spectral+spatial predictor; the few
    coefficients per band are negligible side-info."""
    Z, H, W = cube.shape
    res = [med_residuals(cube[0]).ravel()]
    for z in range(1, Z):
        s = cube[z].astype(np.float64)
        feats = [cube[z - 1].astype(np.float64)]                 # co-located previous band
        N = np.zeros_like(s); N[1:, :] = s[:-1, :]               # spatial up
        Wn = np.zeros_like(s); Wn[:, 1:] = s[:, :-1]             # spatial left
        feats += [N, Wn]
        if z >= 2:
            feats.append(cube[z - 2].astype(np.float64))         # co-located 2-back
        A = np.stack([f.ravel() for f in feats] + [np.ones(H * W)], axis=1)
        y = s.ravel()
        coef, *_ = np.linalg.lstsq(A, y, rcond=None)
        pred = np.rint(A @ coef)
        res.append((y - pred).astype(np.int64))
    return np.concatenate(res)


def entropy_bits(resid):
    """Zero-order entropy of zigzag-mapped residuals (coder-independent predictor-quality measure)."""
    u = ((resid << 1) ^ (resid >> 63)).astype(np.int64)
    vals, cnt = np.unique(u, return_counts=True)
    p = cnt / cnt.sum()
    return float(-(p * np.log2(p)).sum()) * u.size / 8.0     # bytes


def main():
    m = sio.loadmat(MAT)
    cube = np.ascontiguousarray(
        np.asarray(m["indian_pines_corrected"]).transpose(2, 0, 1).astype("<u2"))  # (Z,H,W)
    Z, H, W = cube.shape
    raw = cube.size * 2
    print(f"Indian Pines: {Z} bands {H}x{W} uint16, raw {raw/1e6:.2f} MB")

    lib = load_lib()
    try:
        import imagecodecs as ic
        have_ic = True
    except Exception:
        have_ic = False

    # real per-band codec sizes
    pfc_sz = 0
    for z in range(Z):
        sz, ok = pfc_image(lib, cube[z]); assert ok, "pfc not lossless"
        pfc_sz += sz
    jls_sz = sum(len(ic.jpegls_encode(cube[z])) for z in range(Z)) if have_ic else 0

    # predictor quality, entropy coder held constant (block-adaptive Rice)
    med = med_resid_cube(cube)
    ib = interband_resid(cube)
    c123 = ccsds123_resid(cube)
    rows = [
        ("per-band MED + Rice (spatial only)", rice_bits(med)),
        ("inter-band delta + Rice (spectral-lite)", rice_bits(ib)),
        ("CCSDS-123-class (spectral+spatial LS) + Rice", rice_bits(c123)),
    ]
    print("\n-- predictor quality, Rice coder held constant --")
    for name, b in rows:
        print(f"   {name:46} {raw/b:6.2f}x  ({b/1e6:.2f} MB)")
    print(f"   (entropy-only floors: MED {raw/entropy_bits(med):.2f}x, "
          f"interband {raw/entropy_bits(ib):.2f}x, ccsds123 {raw/entropy_bits(c123):.2f}x)")

    print("\n-- real per-band codecs (spatial only) --")
    print(f"   pfc per-band (arithmetic)                      {raw/pfc_sz:6.2f}x  ({pfc_sz/1e6:.2f} MB)")
    if jls_sz:
        print(f"   JPEG-LS per-band                               {raw/jls_sz:6.2f}x  ({jls_sz/1e6:.2f} MB)")
    print(f"\n   pfc(per-band) vs CCSDS-123-class: {(1 - pfc_sz/rows[2][1])*100:+.1f}%  "
          f"(spectral prediction is the gap)")


if __name__ == "__main__":
    main()
