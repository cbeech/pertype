"""Does the 2D MED predictor beat our current approach on REAL Canon CR2 raw?

CR2 raw is the genuine continuous-tone, sensor-noisy, no-exact-repeats case where
spatial prediction should win (unlike the LZ-friendly /usr/share graphics). The
sensor is a Bayer mosaic (RGGB), so a naive 2D MED would predict each pixel from
differently-coloured neighbours; we deinterleave the 2x2 mosaic into 4 same-colour
sub-planes first, then MED-predict each.

Compares, on held-out crops:
  * raw + zstd/xz, PNG-16 (the general / image baselines);
  * ours-generic        — the current codec on raw Bayer bytes (delta/split gate);
  * MED(Bayer) + ctx    — 2D MED per sub-plane, ctxcoder residuals (prediction only);
  * MED(Bayer) -> codec — MED residual bytes through the full trained codec
                          (prediction + LZ + dictionary).
Round-trip verified on a sample.

Usage: python3 scripts/cr2_med_benchmark.py [crop] [n_files]
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

from compressor import codec, ctxcoder, predictors
from compressor.model import train

CR2_DIR = "/home/user/raws"          # local copy of the NAS pool (processed locally)


def bayer_subplanes(P):
    """Split a 2x2 Bayer plane into its 4 phase sub-planes (same-colour each)."""
    return [P[0::2, 0::2], P[0::2, 1::2], P[1::2, 0::2], P[1::2, 1::2]]


def med_bayer_residual_bytes(P, verify=False):
    """MED-predict each Bayer sub-plane; return concatenated int16 residual bytes."""
    out = []
    for sp in bayer_subplanes(P):
        sp = np.ascontiguousarray(sp).astype(np.int32)
        res = predictors.forward(sp, "med")
        if verify:
            rec = predictors.reconstruct(res, "med")
            assert np.array_equal(rec, sp), "MED round-trip FAILED"
        out.append((res.astype(np.int64) & 0xFFFF).astype(np.uint16).tobytes())
    return b"".join(out)


def med_ctx_size(P, verify=False):
    total = 0
    for sp in bayer_subplanes(P):
        sp = np.ascontiguousarray(sp).astype(np.int32)
        res = predictors.forward(sp, "med")
        blob = ctxcoder.encode(res.reshape(-1))
        total += len(blob)
        if verify:
            dec = np.asarray(ctxcoder.decode(blob, sp.size), dtype=np.int32).reshape(sp.shape)
            assert np.array_equal(predictors.reconstruct(dec, "med"), sp), "round-trip FAILED"
    return total


def sh(cmd, data):
    return len(subprocess.run(cmd, input=data, stdout=subprocess.PIPE).stdout)


def png16(P):
    buf = io.BytesIO()
    Image.fromarray(P.astype("<u2")).save(buf, "PNG", optimize=True)
    return buf.tell()


def main():
    crop = int(sys.argv[1]) if len(sys.argv) > 1 else 512
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 999
    paths = sorted(glob.glob(os.path.join(CR2_DIR, "*.CR2")))[:n]
    crops = []
    for p in paths:
        try:
            with rawpy.imread(p) as raw:
                b = raw.raw_image_visible       # a view into rawpy's buffer...
                h, w = b.shape
                y = ((h - crop) // 2) & ~1
                x = ((w - crop) // 2) & ~1
                # ...so the crop MUST be copied out before the `with` frees it
                crops.append(np.ascontiguousarray(b[y:y + crop, x:x + crop]))
        except Exception as e:
            print(f"  skip {os.path.basename(p)}: {e}", flush=True)
            continue
    print(f"{len(crops)} CR2 crops {crop}x{crop} (16-bit Bayer)", flush=True)
    if len(crops) < 8:
        print("need >=8 usable CR2s"); return

    # disable the cross-image blob on photographic raw (dead weight; see cr2_benchmark)
    import compressor.model as M
    M.BLOB_SPECS = (("none", 0), ("naive", 1 << 12))

    cut = len(crops) * 4 // 5
    tr, te = crops[:cut], crops[cut:]
    print(f"{len(tr)} train + {len(te)} test; training two models...", flush=True)
    m_raw = train([c.tobytes() for c in tr], type_id="cr2raw")
    m_med = train([med_bayer_residual_bytes(c) for c in tr], type_id="cr2med")

    tot = dict(raw=0, png=0, zstd=0, xz=0, ours=0, medctx=0, medcodec=0)
    for i, c in enumerate(te):
        rb = c.tobytes()
        tot["raw"] += len(rb)
        tot["png"] += png16(c)
        tot["zstd"] += sh(["zstd", "-19", "-c"], rb)
        tot["xz"] += sh(["xz", "-9", "-c"], rb)
        tot["ours"] += len(codec.compress(rb, m_raw))
        tot["medctx"] += med_ctx_size(c, verify=(i == 0))
        mrb = med_bayer_residual_bytes(c, verify=(i == 0))
        comp = codec.compress(mrb, m_med)
        assert codec.decompress(comp, m_med) == mrb
        tot["medcodec"] += len(comp)
    n0 = tot["raw"]
    print(f"\nheld-out {len(te)} crops, {n0/1e6:.1f} MB raw  (round-trip verified)")
    order = ["png", "zstd", "xz", "ours", "medctx", "medcodec"]
    best = min(tot[k] for k in order)
    for k in order:
        print(f"  {k:<9}{tot[k]:>11,}  {n0/tot[k]:5.2f}x{'  <- best' if tot[k]==best else ''}")
    print(f"\n  current (ours-generic) {n0/tot['ours']:.2f}x  vs  MED->codec {n0/tot['medcodec']:.2f}x  "
          f"-> MED {'HELPS' if tot['medcodec']<tot['ours'] else 'hurts'} "
          f"({100*(tot['ours']-tot['medcodec'])/tot['ours']:+.1f}%)")


if __name__ == "__main__":
    main()
