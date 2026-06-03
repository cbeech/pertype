"""Benchmark the codec on RAW lossless image data vs gzip/zstd/zstd-dict/PNG.

Each image is decoded to raw RGB pixel bytes — the "raw lossless image". Every
method compresses those identical bytes; PNG (the purpose-built lossless image
codec, and the real competitor) re-encodes the same RGB data. We test two kinds:

  icons  — small images from icon themes (homogeneous: shared palette/style),
           the best case for our cross-image trained dictionary, which per-image
           PNG cannot exploit.
  photo  — larger, heterogeneous graphics, where PNG's spatial prediction filters
           dominate and our byte-LZ has no equivalent.

Usage: python3 scripts/image_benchmark.py [icons|photo]
"""
import hashlib
import io
import os
import sys
import warnings

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
warnings.filterwarnings("ignore")

from PIL import Image

from compressor.benchmark import _gzip_size, _zstd_size, _zstd_dict, _zstd_dict_size
from compressor.codec import compress, decompress
from compressor.model import train

KINDS = {
    "icons": dict(dirs=["/usr/share/icons", "/usr/share/pixmaps"],
                  lo=16, hi=96, raw_lo=500, raw_hi=40_000, want=375),
    # Real photo-sized images, full unrestricted algorithm. Corpus kept modest so
    # the (slow but galloping-accelerated) pure-Python parser completes; the goal
    # is to measure the ACHIEVABLE RATIO, not throughput.
    "photo": dict(dirs=["/usr/share"], lo=80, hi=400,
                  raw_lo=20_000, raw_hi=200_000, want=40, min_disk=4_000,
                  examine=80_000),
}


def collect(kind):
    cfg = KINDS[kind]
    min_disk = cfg.get("min_disk", 0)
    examine_cap = cfg.get("examine", 4000)
    seen, samples = set(), []
    examined = 0
    for base in cfg["dirs"]:
        for root, _, files in os.walk(base):
            for fn in files:
                if not fn.lower().endswith(".png"):
                    continue
                p = os.path.join(root, fn)
                try:
                    if os.path.getsize(p) < min_disk:   # cheap pre-filter
                        continue
                except OSError:
                    continue
                examined += 1
                try:
                    im = Image.open(p)
                    im.load()
                except Exception:
                    continue
                w, h = im.size
                if not (cfg["lo"] <= w <= cfg["hi"] and cfg["lo"] <= h <= cfg["hi"]):
                    continue
                raw = im.convert("RGB").tobytes()
                if not (cfg["raw_lo"] <= len(raw) <= cfg["raw_hi"]):
                    continue
                hsh = hashlib.sha256(raw).hexdigest()
                if hsh in seen:
                    continue
                seen.add(hsh)
                samples.append((raw, (w, h)))
            if len(samples) >= cfg["want"] or examined >= examine_cap:
                break
        if len(samples) >= cfg["want"] or examined >= examine_cap:
            break
    samples.sort(key=lambda s: hashlib.sha256(s[0]).hexdigest())
    return samples


def png_size(raw, size):
    buf = io.BytesIO()
    Image.frombytes("RGB", size, raw).save(buf, "PNG", optimize=True)
    return buf.tell()


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "icons"
    samples = collect(kind)
    if len(samples) < 20:
        print(f"{kind}: only {len(samples)} images found")
        return
    cut = len(samples) * 4 // 5
    import time
    train_s, test_s = samples[:cut], samples[cut:]
    avg = sum(len(r) for r, _ in samples) // len(samples)
    print(f"{kind}: {len(train_s)} train + {len(test_s)} test images "
          f"(avg {avg:,}B raw)", flush=True)

    t0 = time.time()
    model = train([raw for raw, _ in train_s], type_id="img")
    print(f"trained in {time.time() - t0:.0f}s; compressing test set...", flush=True)

    import tempfile
    totals = dict(raw=0, ours=0, gzip=0, zstd=0, zstd_dict=0, png=0)
    with tempfile.TemporaryDirectory() as wd:
        dict_path = _zstd_dict([(None, raw) for raw, _ in train_s], wd)
        for n, (raw, size) in enumerate(test_s, 1):
            t1 = time.time()
            c = compress(raw, model)
            assert decompress(c, model) == raw, "ROUND-TRIP FAILED"
            print(f"  [{n}/{len(test_s)}] {len(raw):,}B -> {len(c):,}B "
                  f"({time.time() - t1:.0f}s)", flush=True)
            totals["raw"] += len(raw)
            totals["ours"] += len(c)
            totals["gzip"] += _gzip_size(raw)
            totals["zstd"] += _zstd_size(raw)
            totals["zstd_dict"] += _zstd_dict_size(raw, dict_path) if dict_path else 0
            totals["png"] += png_size(raw, size)

    raw = totals["raw"]
    print(f"model size (shipped once): {len(model.save()):,} bytes")
    print(f"{'method':<16}{'bytes':>12}{'ratio':>10}")
    print("-" * 38)
    order = ["gzip", "zstd", "zstd_dict", "png", "ours"]
    labels = {"gzip": "gzip -9", "zstd": "zstd -19", "zstd_dict": "zstd -19 +dict",
              "png": "PNG (optimize)", "ours": "ours"}
    print(f"{'raw':<16}{raw:>12,}{1.0:>9.2f}x")
    for k in order:
        v = totals[k]
        print(f"{labels[k]:<16}{v:>12,}{(raw / v if v else 0):>9.2f}x")


if __name__ == "__main__":
    main()
