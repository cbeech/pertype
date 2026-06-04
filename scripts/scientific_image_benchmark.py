"""Benchmark the image codec on real scientific/medical images (DICOM + FITS).

Sample data (public, downloaded locally to ~/sample_data — not committed). To fetch::

  mkdir -p ~/sample_data/{dicom,fits}
  # DICOM 16-bit CT/MR (pydicom test files)
  base=https://github.com/pydicom/pydicom/raw/main/src/pydicom/data/test_files
  for f in CT_small.dcm MR_small.dcm 693_J2KI.dcm emri_small.dcm; do
      curl -sSL -o ~/sample_data/dicom/$f $base/$f; done
  # FITS int16 + float32 (NASA sample archive)
  for f in UITfuv2582gc.fits FOCx38i0101t_c0f.fits; do
      curl -sS -o ~/sample_data/fits/$f https://fits.gsfc.nasa.gov/samples/$f; done
  pip install --user pydicom Pillow

The 16-bit integer images go through the codec's gray mode (2D MED/GAP/CALIC). The
honest split this draws out: DICOM medical is dense continuous-tone (smooth tissue +
edges) — the predictor's domain, a clear win; sparse astronomy is dominated by an
exact-zero background that LZ (zstd/xz/PNG-deflate) run-length-crushes but a
prediction-only codec can't — the same predict-vs-LZ boundary as graphics. Float FITS
is near the entropy floor for everyone.

Needs pydicom + Pillow. Usage: python3 scripts/scientific_image_benchmark.py
"""
import glob
import io
import os
import subprocess
import sys
import warnings

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
warnings.filterwarnings("ignore")

from PIL import Image

from compressor import imagecodec

DATA = os.path.expanduser("~/sample_data")


def sh(cmd, data):
    return len(subprocess.run(cmd, input=data, stdout=subprocess.PIPE).stdout)


def png16(u16):
    buf = io.BytesIO()
    Image.fromarray(u16.astype("<u2")).save(buf, "PNG", optimize=True)
    return buf.tell()


def read_fits(path):
    """Minimal FITS primary-HDU reader: 2880-byte header blocks, then big-endian data."""
    raw = open(path, "rb").read()
    off, hdr = 0, {}
    while True:
        blk = raw[off:off + 2880]; off += 2880; end = False
        for i in range(0, 2880, 80):
            c = blk[i:i + 80].decode("ascii", "replace"); k = c[:8].strip()
            if k == "END":
                end = True; break
            if "=" in c:
                hdr[k] = c[9:].split("/")[0].strip()
        if end:
            break
    bp = int(hdr["BITPIX"]); na = int(hdr["NAXIS"])
    dims = [int(hdr[f"NAXIS{i}"]) for i in range(1, na + 1)]
    if not dims:
        return bp, None
    dt = {8: ">u1", 16: ">i2", 32: ">i4", -32: ">f4", -64: ">f8"}[bp]
    n = int(np.prod(dims))
    a = np.frombuffer(raw[off:off + n * abs(bp) // 8], dt).reshape(dims[::-1])
    # FITS is big-endian; work in native byte order so the codec's little-endian
    # output round-trips byte-exact.
    return bp, np.ascontiguousarray(a.astype(a.dtype.newbyteorder("=")))


def bench_gray(name, planes):
    raw = ours = png = z = x = 0
    ok = True
    for a in planes:
        a = np.ascontiguousarray(a)               # pass signed int16 AS-IS (the codec
        e = imagecodec.encode(a, bayer=False)     # handles sign; viewing as uint16 would
        dec = imagecodec.decode(e)                # wrap negatives and wreck prediction)
        if not np.array_equal(dec.view(a.dtype), a):
            ok = False
        rb = a.tobytes()
        raw += len(rb); ours += len(e)
        png += png16(a.view(np.uint16) if a.dtype == np.int16 else a)
        z += sh(["zstd", "-19", "-c"], rb); x += sh(["xz", "-9", "-c"], rb)
    best = min(png, z, x, ours)
    print(f"\n{name}: {len(planes)} images, {raw:,} B  (round-trip {ok})")
    for label, sz in (("PNG-16", png), ("zstd -19", z), ("xz -9", x), ("ours (gray)", ours)):
        print(f"  {label:<12}{sz:>11,}  {raw / sz:5.2f}x{'  <- best' if sz == best else ''}")


def dicom_frames():
    import pydicom
    out = []
    for p in sorted(glob.glob(os.path.join(DATA, "dicom", "*.dcm"))):
        try:
            ds = pydicom.dcmread(p, force=True)
            a = ds.pixel_array
        except Exception:
            continue
        if a.dtype != np.int16:
            continue
        if a.ndim == 2:
            out.append(a)
        elif a.ndim == 3:                      # multiframe -> individual slices
            out.extend(a[i] for i in range(a.shape[0]))
    return out


def main():
    frames = dicom_frames()
    if frames:
        bench_gray("DICOM 16-bit medical (CT/MR)", frames)
    fint, ffloat = [], []
    for p in sorted(glob.glob(os.path.join(DATA, "fits", "*.fits"))):
        bp, d = read_fits(p)
        if d is None or d.ndim != 2:
            continue
        (fint if bp == 16 else ffloat if bp in (-32, -64) else []).append(d)
    if fint:
        bench_gray("FITS int16 (astronomy)", fint)
    for d in ffloat:
        fb = np.ascontiguousarray(d.astype("<f4")).tobytes()
        print(f"\nFITS float32 {d.shape}: {len(fb):,} B  "
              f"zstd {len(fb) / sh(['zstd', '-19', '-c'], fb):.2f}x  "
              f"xz {len(fb) / sh(['xz', '-9', '-c'], fb):.2f}x  (near the entropy floor)")


if __name__ == "__main__":
    main()
