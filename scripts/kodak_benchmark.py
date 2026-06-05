"""Kodak lossless image set: our RGB image codec vs PNG / JPEG-XL / WebP (all lossless).

The Kodak set (24 768x512 photographs) is the standard public benchmark for lossless
*image* codecs. Our `imagecodec` (per-plane MED/CALIC + context coding) is built for
continuous-tone images, so this is the named test for it. Source:
  for i in $(seq -w 1 24); do curl -O https://r0k.us/graphics/kodak/kodak/kodim$i.png; done

Reports bits-per-pixel (the standard image metric) and ratio vs the raw RGB bytes;
ours is round-trip verified per image before its size is counted.

Usage: python3 scripts/kodak_benchmark.py <dir-of-kodim*.png>
"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from PIL import Image

from compressor import imagecodec

try:
    import imagecodecs as ic
except Exception:
    ic = None


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    paths = sorted(glob.glob(os.path.join(sys.argv[1], "kodim*.png")))
    if not paths:
        sys.exit("no kodim*.png found")
    tot = {"raw": 0, "png": 0, "jxl": 0, "webp": 0, "ours": 0}
    n_pix = 0
    print(f"{'image':<10}{'raw':>9}{'PNG':>8}{'JXL':>8}{'WebP':>8}{'ours':>8}"
          f"{'ours bpp':>10}{'vs PNG':>9}")
    print("-" * 70)
    wins = 0
    for p in paths:
        arr = np.asarray(Image.open(p).convert("RGB"), dtype=np.uint8)
        H, W = arr.shape[:2]
        raw = H * W * 3
        blob = imagecodec.encode(arr)
        assert np.array_equal(imagecodec.decode(blob), arr), f"round-trip FAILED {p}"
        import io
        b = io.BytesIO(); Image.fromarray(arr).save(b, "PNG", optimize=True)
        png = len(b.getvalue())
        jxl = len(ic.jpegxl_encode(arr, lossless=True)) if ic else 0
        webp = len(ic.webp_encode(arr, level=100, lossless=True)) if ic else 0
        ours = len(blob)
        for k, v in (("raw", raw), ("png", png), ("jxl", jxl), ("webp", webp), ("ours", ours)):
            tot[k] += v
        n_pix += H * W
        wins += ours < png
        name = os.path.basename(p).replace(".png", "")
        print(f"{name:<10}{raw/1e3:>8.0f}K{png/1e3:>7.0f}K{jxl/1e3:>7.0f}K"
              f"{webp/1e3:>7.0f}K{ours/1e3:>7.0f}K{8*ours/(H*W):>9.3f} "
              f"{(png-ours)/png*100:>+7.0f}%")
    print("-" * 70)

    def line(k, label):
        return f"{label:<14}{tot[k]/1e6:>7.2f} MB   {tot['raw']/tot[k]:5.2f}x   {8*tot[k]/n_pix:.3f} bpp"
    print("TOTALS (24 images):")
    for k, lbl in [("png", "PNG"), ("jxl", "JPEG-XL"), ("webp", "WebP-LL"), ("ours", "ours")]:
        if tot[k]:
            print("  " + line(k, lbl))
    print(f"  ours beats PNG on {wins}/24; ours vs PNG total {(tot['png']-tot['ours'])/tot['png']*100:+.1f}%, "
          f"vs JXL {(tot['jxl']-tot['ours'])/tot['jxl']*100:+.1f}%")


if __name__ == "__main__":
    main()
