"""Benchmark the codec on REAL Canon CR2 raw sensor data.

A CR2 is a TIFF container whose sensor data is already losslessly compressed
(Canon lossless JPEG). We decode it with rawpy/LibRaw to the raw Bayer sensor
array (16-bit) — the true "raw lossless image" — and compress an aligned center
crop with each method. PNG-16 (grayscale) is the lossless-image baseline.

Photographic raw is the worst case for a byte-LZ + dictionary compressor (high
sensor noise, mosaiced channels, no spatial prediction), so this measures the
floor of what the algorithm achieves. Everything stays local to this machine.

Usage: python3 scripts/cr2_benchmark.py [crop] [n_files]
"""
import glob
import hashlib
import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import rawpy
from PIL import Image

from compressor.benchmark import _gzip_size, _zstd_size, _zstd_dict, _zstd_dict_size
from compressor.codec import compress, decompress
from compressor.model import train

CR2_DIR = os.environ.get("CR2_DIR", "data/raw")


def collect(crop, n_files):
    paths = sorted(glob.glob(os.path.join(CR2_DIR, "*.CR2")))[:n_files]
    samples, full_ref = [], []
    for p in paths:
        try:
            with rawpy.imread(p) as raw:
                bayer = raw.raw_image_visible  # 2D uint16 sensor data
                h, w = bayer.shape
                # center crop, aligned to the 2x2 Bayer phase
                y = ((h - crop) // 2) & ~1
                x = ((w - crop) // 2) & ~1
                c = np.ascontiguousarray(bayer[y:y + crop, x:x + crop])
                full_ref.append((h * w * 2, os.path.getsize(p)))  # uncompressed vs CR2
        except Exception as e:
            print(f"  skip {os.path.basename(p)}: {e}", flush=True)
            continue
        samples.append(c)
    samples.sort(key=lambda a: hashlib.sha256(a.tobytes()).hexdigest())
    return samples, full_ref


def png16_size(crop2d):
    buf = io.BytesIO()
    Image.fromarray(crop2d.astype("<u2")).save(buf, "PNG", optimize=True)
    return buf.tell()


def main():
    crop = int(sys.argv[1]) if len(sys.argv) > 1 else 256
    n_files = int(sys.argv[2]) if len(sys.argv) > 2 else 24
    print(f"decoding up to {n_files} CR2 files, {crop}x{crop} Bayer crops...", flush=True)
    samples, full_ref = collect(crop, n_files)
    if len(samples) < 8:
        print(f"only {len(samples)} usable CR2s found")
        return

    # Reference: Canon's own full-frame lossless ratio (uncompressed vs .CR2 file).
    unc = sum(u for u, _ in full_ref)
    cr2 = sum(c for _, c in full_ref)
    print(f"reference — full-frame Canon lossless: {unc/cr2:.2f}x "
          f"(uncompressed {unc:,}B vs CR2 {cr2:,}B)", flush=True)

    if crop >= 512:
        # On photographic raw the cross-image dictionary/blob is dead weight
        # (zstd+dict < zstd), and a big blob makes the parse intractable at this
        # size. Disable it; in-file LZ + repeat offsets + cost-optimal parse
        # (chain 128) stay at full strength — that is what matters on raw.
        import compressor.model as M
        M.BLOB_SPECS = (("none", 0), ("naive", 1 << 12))
        print("note: blob disabled for large crop (dead weight on raw); in-file LZ "
              "+ repeat offsets + cost-optimal parse remain full-strength", flush=True)

    cut = len(samples) * 4 // 5
    train_s, test_s = samples[:cut], samples[cut:]
    raws = [c.tobytes() for c in train_s]
    print(f"{len(train_s)} train + {len(test_s)} test crops "
          f"({len(raws[0]):,}B raw each); training...", flush=True)
    model = train(raws, type_id="cr2")

    totals = dict(raw=0, ours=0, gzip=0, zstd=0, zstd_dict=0, png=0)
    with tempfile.TemporaryDirectory() as wd:
        dict_path = _zstd_dict([(None, r) for r in raws], wd)
        for n, c in enumerate(test_s, 1):
            data = c.tobytes()
            comp = compress(data, model)
            assert decompress(comp, model) == data, "ROUND-TRIP FAILED"
            totals["raw"] += len(data)
            totals["ours"] += len(comp)
            totals["gzip"] += _gzip_size(data)
            totals["zstd"] += _zstd_size(data)
            totals["zstd_dict"] += _zstd_dict_size(data, dict_path) if dict_path else 0
            totals["png"] += png16_size(c)
            print(f"  [{n}/{len(test_s)}] ours {len(data)/len(comp):.2f}x", flush=True)

    raw = totals["raw"]
    print(f"\nmodel size (shipped once): {len(model.save()):,} bytes")
    print(f"{'method':<16}{'bytes':>12}{'ratio':>10}")
    print("-" * 38)
    print(f"{'raw':<16}{raw:>12,}{1.0:>9.2f}x")
    for k, label in [("gzip", "gzip -9"), ("zstd", "zstd -19"),
                     ("zstd_dict", "zstd -19 +dict"), ("png", "PNG-16"), ("ours", "ours")]:
        v = totals[k]
        print(f"{label:<16}{v:>12,}{(raw / v if v else 0):>9.2f}x")


if __name__ == "__main__":
    main()
