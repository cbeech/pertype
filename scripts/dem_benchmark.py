"""Digital elevation models (terrain rasters): our gray image codec vs PNG-16 / zstd / xz.

DEMs are smooth, strongly autocorrelated 16-bit height fields — the continuous-tone
case prediction is built for. Input is an SRTM ``.hgt`` tile (headerless big-endian
int16, side = sqrt(bytes/2); SRTM1 = 3601, SRTM3 = 1201). Public no-auth source:
the AWS "terrain tiles" open dataset, e.g.
  curl -L -o N45E006.hgt.gz \\
    https://s3.amazonaws.com/elevation-tiles-prod/skadi/N45/N45E006.hgt.gz && gunzip N45E006.hgt.gz

Usage: python3 scripts/dem_benchmark.py <tile.hgt> [<tile2.hgt> ...]
Every codec's size is reported as a ratio vs the raw int16 bytes; ours is round-trip
verified (byte-exact) before its number is trusted.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from compressor import imagecodec


def load_hgt(path):
    raw = open(path, "rb").read()
    n = int(round((len(raw) // 2) ** 0.5))
    if n * n * 2 != len(raw):
        sys.exit(f"{path}: not a square int16 .hgt ({len(raw)} bytes)")
    return np.frombuffer(raw, ">i2").reshape(n, n).astype(np.int16)


def png16_size(a):
    import io

    from PIL import Image
    # 16-bit PNG is unsigned; shift signed -> unsigned by +32768 (lossless, reversible)
    u = (a.astype(np.int32) + 32768).astype(np.uint16)
    buf = io.BytesIO()
    Image.fromarray(u, mode="I;16").save(buf, format="PNG", optimize=True)
    return len(buf.getvalue())


def ext_size(data, cmd):
    p = subprocess.run(cmd, input=data, stdout=subprocess.PIPE, check=True)
    return len(p.stdout)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    print(f"{'tile':<14}{'raw':>10}{'PNG16':>9}{'zstd19':>9}{'xz9':>8}{'ours':>9}"
          f"{'ours/best':>11}")
    print("-" * 70)
    for path in sys.argv[1:]:
        a = load_hgt(path)
        raw = a.astype(">i2").tobytes()
        blob = imagecodec.encode(a, bayer=False)
        assert np.array_equal(imagecodec.decode(blob), a), f"round-trip FAILED {path}"
        png = png16_size(a)
        zst = ext_size(raw, ["zstd", "-19", "-c"])
        xzs = ext_size(raw, ["xz", "-9", "-c"])
        ours = len(blob)
        best_other = min(png, zst, xzs)
        name = os.path.basename(path)[:13]
        print(f"{name:<14}{len(raw)/1e6:>9.1f}M{png/1e6:>8.2f}M{zst/1e6:>8.2f}M"
              f"{xzs/1e6:>7.2f}M{ours/1e6:>8.2f}M"
              f"{best_other/ours:>10.2f}x")
        print(f"{'':<14}ratios vs raw:  PNG16 {len(raw)/png:.2f}x  zstd {len(raw)/zst:.2f}x  "
              f"xz {len(raw)/xzs:.2f}x  ours {len(raw)/ours:.2f}x"
              f"   {'WIN' if ours < best_other else 'lose'}")


if __name__ == "__main__":
    main()
