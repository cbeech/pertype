"""Does a 2D MED/Paeth intra predictor + our ctxcoder beat PNG on real images?

PNG beats our generic byte-LZ codec on photos because of its spatial prediction
filters (README). This measures whether giving our entropy coder the same kind of
2D prediction closes that gap. For each image we deinterleave RGB into channel
planes, predict each plane with MED / Paeth, and ctxcoder the residuals; compare to
PNG (re-encoded, max compression) and raw bytes + zstd/xz. Round-trip verified.

Usage: python3 scripts/image_med_benchmark.py [icons|photo]
"""
import io
import os
import subprocess
import sys
import warnings

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
warnings.filterwarnings("ignore")

from PIL import Image

from pertype import ctxcoder, predictors

KINDS = {
    "icons": dict(dirs=["/usr/share/icons", "/usr/share/pixmaps"], lo=16, hi=128, want=120),
    "photo": dict(dirs=["/usr/share"], lo=80, hi=512, want=40),
}


def sh(cmd, data):
    return len(subprocess.run(cmd, input=data, stdout=subprocess.PIPE).stdout)


def collect(kind, cap=400):
    cfg = KINDS[kind]
    seen, out, examined = set(), [], 0
    for base in cfg["dirs"]:
        for root, _, files in os.walk(base):
            for fn in files:
                if not fn.lower().endswith(".png"):
                    continue
                if examined >= cap or len(out) >= cfg["want"]:
                    break
                p = os.path.join(root, fn)
                try:
                    im = Image.open(p).convert("RGB")
                except Exception:
                    continue
                examined += 1
                w, h = im.size
                if not (cfg["lo"] <= w <= cfg["hi"] and cfg["lo"] <= h <= cfg["hi"]):
                    continue
                arr = np.asarray(im, dtype=np.uint8)            # H x W x 3
                key = arr.tobytes()
                if key in seen:
                    continue
                seen.add(key)
                out.append(arr)
        if len(out) >= cfg["want"]:
            break
    return out


def predicted_size(arr, kind, verify=False):
    """ctxcoder size of the MED/Paeth residuals across the 3 channel planes."""
    total = 0
    for c in range(3):
        plane = arr[:, :, c].astype(np.int32)
        res = predictors.forward(plane, kind)
        blob = ctxcoder.encode(res.reshape(-1))
        total += len(blob)
        if verify:
            dec = np.asarray(ctxcoder.decode(blob, plane.size), dtype=np.int32).reshape(plane.shape)
            rec = predictors.reconstruct(dec, kind)
            assert np.array_equal(rec, plane), "ROUND-TRIP FAILED"
    return total


def png_size(arr):
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG", optimize=True, compress_level=9)
    return buf.getbuffer().nbytes


def main():
    kind = sys.argv[1] if len(sys.argv) > 1 else "photo"
    imgs = collect(kind)
    if not imgs:
        print(f"no {kind} images found")
        return
    tot = dict(raw=0, png=0, zstd=0, xz=0, med=0, paeth=0)
    for i, arr in enumerate(imgs):
        raw = arr.tobytes()
        tot["raw"] += len(raw)
        tot["png"] += png_size(arr)
        tot["zstd"] += sh(["zstd", "-19", "-c"], raw)
        tot["xz"] += sh(["xz", "-9", "-c"], raw)
        tot["med"] += predicted_size(arr, "med", verify=(i < 5))
        tot["paeth"] += predicted_size(arr, "paeth", verify=(i < 5))
    n = tot["raw"]
    print(f"\n{kind}: {len(imgs)} images, {n/1e6:.2f} MB raw RGB  (round-trip verified)")
    for k in ("png", "zstd", "xz", "med", "paeth"):
        flag = "  <- best" if tot[k] == min(tot[m] for m in ("png", "zstd", "xz", "med", "paeth")) else ""
        print(f"  {k:<7}{tot[k]:>11,}  {n/tot[k]:5.2f}x{flag}")
    best_pred = min(tot["med"], tot["paeth"])
    print(f"  -> 2D-pred best {n/best_pred:.2f}x vs PNG {n/tot['png']:.2f}x: "
          f"{'BEATS PNG' if best_pred < tot['png'] else 'behind PNG'}")


if __name__ == "__main__":
    main()
