"""Compress a FULL-resolution Canon raw Bayer frame with our codec.

The cost-optimal parser holds O(N) state, so a 42 MB frame in one pass is
multi-GB and slow. A real port would block-process large inputs (as zstd/gzip
window internally), so we do the same: compress the full frame in 2 MB blocks
with the real codec and sum. On raw (matches are short/local) block boundaries
barely matter. Compared against Canon's own CR2, gzip, zstd, and PNG-16.

Everything uses ONLY the target file: the entropy model is trained on small
chunks sampled from this frame's own Bayer data (like gzip/zstd adapting to the
file). The cross-image blob is disabled (dead weight on raw). Local only.

Usage: python3 scripts/full_raw_benchmark.py [target_index] [block_MB]
"""
import glob
import io
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import rawpy
from PIL import Image

from compressor.benchmark import _gzip_size, _zstd_size
from compressor.codec import compress, decompress
import compressor.model as M

CR2_DIR = os.environ.get("CR2_DIR", "data/raw")


def main():
    ti = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    blk = (int(sys.argv[2]) if len(sys.argv) > 2 else 2) << 20

    paths = sorted(glob.glob(os.path.join(CR2_DIR, "*.CR2")))
    target = paths[ti]

    with rawpy.imread(target) as r:
        bayer = np.ascontiguousarray(r.raw_image_visible)
    full = bayer.tobytes()
    n = len(full)

    # Train the entropy model on small chunks sampled across THIS frame.
    chunk, n_train = 256 * 1024, 12
    step = max(chunk, n // n_train)
    train_chunks = [full[i:i + chunk] for i in range(0, n - chunk, step)][:n_train]
    print(f"training on {len(train_chunks)} chunks of this frame's own raw data...",
          flush=True)
    M.BLOB_SPECS = (("none", 0), ("naive", 1 << 12))  # blob useless on raw
    model = M.train(train_chunks, type_id="cr2raw")
    nblk = -(-n // blk)
    print(f"target {os.path.basename(target)}: full raw {n:,}B, {nblk} x {blk >> 20}MB blocks",
          flush=True)

    ours = 0
    t0 = time.time()
    for i in range(0, n, blk):
        b = full[i:i + blk]
        c = compress(b, model)
        assert decompress(c, model) == b, "ROUND-TRIP FAILED"
        ours += len(c)
        print(f"  block {i // blk + 1}/{nblk}: {len(b):,}->{len(c):,} "
              f"({time.time() - t0:.0f}s elapsed)", flush=True)

    gz = _gzip_size(full)
    zs = _zstd_size(full)
    buf = io.BytesIO()
    Image.fromarray(bayer.astype("<u2")).save(buf, "PNG", compress_level=9)
    png = buf.tell()
    cr2 = os.path.getsize(target)

    print(f"\nfull-frame {n:,}B raw Bayer ({os.path.basename(target)}):")
    print(f"  {'method':<24}{'bytes':>13}{'ratio':>8}")
    print("  " + "-" * 45)
    for label, v in [("CR2 file (Canon lossless)", cr2), ("gzip -9", gz),
                     ("zstd -19", zs), ("PNG-16", png), ("ours (blocked)", ours)]:
        print(f"  {label:<24}{v:>13,}{n / v:>7.2f}x")


if __name__ == "__main__":
    main()
