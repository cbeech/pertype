"""Measure-first: depth / disparity / optical-flow fields (target #7).

Robotics/AR depth & disparity maps are piecewise-smooth integers — smooth interiors, sharp
object-boundary edges, occlusion holes (value 0). They're stored/transported as PNG, WebP-
lossless, or raw LZ4 (in rosbags) — predictors that are weak (PNG = Paeth + deflate) or absent
(LZ4). Our 2D image codec (edge-aware MED/CALIC predictor + context arithmetic) should beat them.

Bar: PNG (Paeth + DEFLATE) and WebP-lossless — the depth-storage incumbents; zstd/xz for context.
Ours: `imagecodec.encode` (per map), round-trip verified.

Data: a directory of disparity/depth PNGs. Default = Middlebury 2006 stereo disparity maps
(real, piecewise-smooth, uint8). Download a scene:
  B=https://vision.middlebury.edu/stereo/data/scenes2006/HalfSize/zip-2views
  curl -O $B/Aloe-2views.zip && unzip Aloe-2views.zip   # gives Aloe/disp1.png, disp5.png
  DEPTH_DIR=. python scripts/depth_benchmark.py
"""
import glob
import io
import os
import subprocess

import numpy as np
from PIL import Image

from pertype import imagecodec

DIR = os.environ.get("DEPTH_DIR", "data/depth")


def sh(cmd, data):
    return len(subprocess.run(cmd, input=data, stdout=subprocess.PIPE).stdout)


def png_size(a):
    b = io.BytesIO(); Image.fromarray(a).save(b, "PNG", optimize=True); return b.tell()


def webp_size(a):
    if a.dtype != np.uint8:
        return None  # WebP is 8-bit only
    b = io.BytesIO(); Image.fromarray(a).save(b, "WebP", lossless=True); return b.tell()


def main():
    paths = sorted(glob.glob(os.path.join(DIR, "**", "disp*.png"), recursive=True)) \
        or sorted(glob.glob(os.path.join(DIR, "**", "*.png"), recursive=True))
    if not paths:
        raise SystemExit(f"no PNGs under {DIR} (set DEPTH_DIR)")
    tot = dict(raw=0, zstd=0, xz=0, png=0, webp=0, ours=0)
    print(f"{'map':<22}{'raw KB':>8}{'PNG':>7}{'WebP':>7}{'ours':>7}{'vsPNG':>8}")
    for p in paths:
        a = np.ascontiguousarray(np.array(Image.open(p)))
        if a.ndim != 2:
            continue
        raw = a.tobytes(); n = len(raw)
        png = png_size(a); webp = webp_size(a)
        e = imagecodec.encode(a, bayer=False)
        assert np.array_equal(imagecodec.decode(e), a), f"round-trip FAILED {p}"
        tot["raw"] += n; tot["png"] += png; tot["webp"] += (webp or png)
        tot["ours"] += len(e)
        tot["zstd"] += sh(["zstd", "-19", "-c"], raw); tot["xz"] += sh(["xz", "-9", "-c"], raw)
        name = os.path.relpath(p, DIR)
        wv = f"{n/webp:.2f}x" if webp else "  n/a"
        print(f"{name:<22}{n/1e3:>8.0f}{n/png:>6.2f}x{wv:>7}{n/len(e):>6.2f}x"
              f"{(png-len(e))/png*100:>+7.0f}%")
    R = tot["raw"]
    print(f"\n{'TOTAL ratios:':<16} zstd {R/tot['zstd']:.2f}x   xz {R/tot['xz']:.2f}x   "
          f"PNG {R/tot['png']:.2f}x   WebP-LL {R/tot['webp']:.2f}x   ours {R/tot['ours']:.2f}x")
    print(f"ours vs PNG bar: {(tot['png']-tot['ours'])/tot['png']*100:+.1f}%   "
          f"vs WebP-LL: {(tot['webp']-tot['ours'])/tot['webp']*100:+.1f}%   "
          f"({'WIN' if tot['ours'] < min(tot['png'], tot['webp']) else 'lose'})   round-trip OK")


if __name__ == "__main__":
    main()
