"""Benchmark the shipped raw/photo image codec (`compressor.imagecodec`).

Reproduces the README image numbers from the actual codec (not a transform proxy):
for each Canon CR2 it compresses the **Bayer** sensor plane and the **demosaiced RGB**
image, round-trip verifying both, and compares to PNG / zstd / xz and Canon's own
in-camera lossless `.CR2`. The codec is MED/GAP/CALIC per-plane selection — no LZ, no
trained model.

Needs `rawpy` and Pillow. CR2s default to ~/raws (copy them there from wherever
your raws live — they are processed locally only).

Usage: python3 scripts/imagecodec_benchmark.py [n_files] [cr2_dir]
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

import rawpy
from PIL import Image

from compressor import imagecodec

CR2_DIR = os.path.expanduser("~/raws")


def sh(cmd, data):
    return len(subprocess.run(cmd, input=data, stdout=subprocess.PIPE).stdout)


def png(arr):
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG", optimize=True, compress_level=9)
    return buf.getbuffer().nbytes


def report(name, tot, extra=()):
    raw = tot["raw"]
    print(f"\n{name}: {tot['n']} frames, {raw / 1e6:.0f} MB raw  (round-trip verified)")
    rows = [("PNG", tot["png"]), ("zstd -19", tot["zstd"]), ("xz -9", tot["xz"])]
    rows += list(extra)
    rows.append(("ours (RIMG)", tot["ours"]))
    best = min(sz for _, sz in rows)
    for label, sz in rows:
        flag = "  <- best" if sz == best else ""
        print(f"  {label:<14}{sz:>13,}  {raw / sz:5.2f}x{flag}")


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    cr2_dir = sys.argv[2] if len(sys.argv) > 2 else CR2_DIR
    paths = sorted(glob.glob(os.path.join(cr2_dir, "*.CR2")))[:n]
    if not paths:
        print(f"no CR2 files in {cr2_dir}")
        return

    bayer = {k: 0 for k in ("n", "raw", "png", "zstd", "xz", "ours", "cr2")}
    rgb = {k: 0 for k in ("n", "raw", "png", "zstd", "xz", "ours")}
    for p in paths:
        with rawpy.imread(p) as raw:
            b = np.ascontiguousarray(raw.raw_image_visible)               # uint16 Bayer
            im = np.ascontiguousarray(raw.postprocess(
                use_camera_wb=True, output_bps=8, no_auto_bright=True))   # uint8 RGB

        eb = imagecodec.encode(b, bayer=True)
        assert np.array_equal(imagecodec.decode(eb), b), f"Bayer round-trip {p}"
        bayer["n"] += 1; bayer["raw"] += b.nbytes; bayer["ours"] += len(eb)
        bayer["cr2"] += os.path.getsize(p)
        bb = b.astype("<u2").tobytes()
        bayer["png"] += png(b.astype("<u2")); bayer["zstd"] += sh(["zstd", "-19", "-c"], bb)
        bayer["xz"] += sh(["xz", "-9", "-c"], bb)

        er = imagecodec.encode(im)
        assert np.array_equal(imagecodec.decode(er), im), f"RGB round-trip {p}"
        rgb["n"] += 1; rgb["raw"] += im.nbytes; rgb["ours"] += len(er)
        rb = im.tobytes()
        rgb["png"] += png(im); rgb["zstd"] += sh(["zstd", "-19", "-c"], rb)
        rgb["xz"] += sh(["xz", "-9", "-c"], rb)

    report("Bayer raw (16-bit)", bayer, extra=[("Canon .CR2", bayer["cr2"])])
    report("demosaiced RGB (8-bit)", rgb)


if __name__ == "__main__":
    main()
