"""Multi-frame ours-vs-JPEG-XL comparison on full-resolution Canon raw.

For each frame: decode to the 16-bit Bayer array, self-train a model on chunks of
that frame, compress the whole frame in 2 MB blocks (transform + entropy; LZ
disabled because it adds ~nothing on decorrelated raw and is ~15x slower), and
compare against JPEG XL lossless (cjxl -d 0 -e7, via libjxl) and Canon's own CR2.

Every block is round-trip verified (assert decompress(compress(b)) == b), and the
"ours+model" column counts the shipped model, so the single-file ratio isn't
inflated. Frames read from a LOCAL copy, not the NAS.
"""
import glob
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import rawpy
import imagecodecs

import compressor.model as M
from compressor.codec import compress, decompress

LOCAL_DIR = "/home/user/raws"
M.BLOB_SPECS = (("none", 0),)  # transform + entropy only; LZ adds ~0 on raw


def jxl_size(bayer, effort=7):
    enc = imagecodecs.jpegxl_encode(bayer, lossless=True, effort=effort)
    dec = imagecodecs.jpegxl_decode(enc).reshape(bayer.shape).astype(np.uint16)
    assert np.array_equal(dec, bayer), "JXL not lossless!"
    return len(enc)


def main():
    paths = sorted(glob.glob(os.path.join(LOCAL_DIR, "*.CR2")))
    hdr = f"{'frame':<15}{'rawMB':>7}{'Canon':>7}{'JXL':>7}{'ours':>7}{'ours+m':>8}{'min':>6}"
    print(hdr, flush=True)
    print("-" * len(hdr), flush=True)
    rows = []
    for p in paths:
        t0 = time.time()
        with rawpy.imread(p) as r:
            bayer = np.ascontiguousarray(r.raw_image_visible.astype(np.uint16))
        full = bayer.tobytes()
        n = len(full)

        chunk = 256 * 1024
        tr = [full[i:i + chunk] for i in range(0, n - chunk, n // 12)][:12]
        model = M.train(tr, type_id="cr2")
        model_sz = len(model.save())

        blk = 2 << 20
        ours = 0
        for i in range(0, n, blk):
            b = full[i:i + blk]
            c = compress(b, model)
            assert decompress(c, model) == b, "ROUND-TRIP FAILED"
            ours += len(c)

        jx = jxl_size(bayer)
        cr2 = os.path.getsize(p)
        r = dict(canon=n / cr2, jxl=n / jx, ours=n / ours, oursm=n / (ours + model_sz))
        rows.append(r)
        print(f"{os.path.basename(p):<15}{n/1e6:>7.1f}{r['canon']:>7.2f}{r['jxl']:>7.2f}"
              f"{r['ours']:>7.2f}{r['oursm']:>8.2f}{(time.time()-t0)/60:>6.1f}", flush=True)

    print("-" * len(hdr), flush=True)
    mean = {k: statistics.mean(row[k] for row in rows) for k in rows[0]}
    print(f"{'MEAN':<15}{'':>7}{mean['canon']:>7.2f}{mean['jxl']:>7.2f}"
          f"{mean['ours']:>7.2f}{mean['oursm']:>8.2f}", flush=True)
    wins = sum(1 for row in rows if row["oursm"] > row["jxl"])
    print(f"\nours+model beats JXL on {wins}/{len(rows)} frames", flush=True)


if __name__ == "__main__":
    main()
