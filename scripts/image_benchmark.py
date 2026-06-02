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
    # Photos kept small + few: our pure-Python cost-optimal parser is ~O(n·chain·
    # matchlen) per file and does not scale to large images. The PNG/zstd baselines
    # are unaffected, so the comparison stays valid.
    "photo": dict(dirs=["/usr/share"], lo=64, hi=160,
                  raw_lo=8_000, raw_hi=24_000, want=50, min_disk=1_500),
}


def collect(kind):
    cfg = KINDS[kind]
    min_disk = cfg.get("min_disk", 0)
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
            if len(samples) >= cfg["want"] or examined >= 4000:
                break
        if len(samples) >= cfg["want"] or examined >= 4000:
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
    if kind == "photo":  # keep our slow parser tractable on big spatial data
        import compressor.model as M
        M.MAX_CHAIN = 32
        M.BLOB_SPECS = (("none", 0), ("cover", 1 << 14), ("naive", 1 << 14))

    cut = len(samples) * 4 // 5
    train_s, test_s = samples[:cut], samples[cut:]
    print(f"{kind}: {len(train_s)} train + {len(test_s)} test images")

    model = train([raw for raw, _ in train_s], type_id="img")

    import tempfile
    totals = dict(raw=0, ours=0, gzip=0, zstd=0, zstd_dict=0, png=0)
    with tempfile.TemporaryDirectory() as wd:
        dict_path = _zstd_dict([(None, raw) for raw, _ in train_s], wd)
        for raw, size in test_s:
            c = compress(raw, model)
            assert decompress(c, model) == raw, "ROUND-TRIP FAILED"
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
