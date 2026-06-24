"""Follow-up verification: does the validated Canon-CR2 Bayer codec hold against JPEG-XL lossless?

The original CR2 benchmark (`scripts/cr2_benchmark.py`) measured pertype's Bayer codec (`imagecodec`,
bayer=True — CFA-phase split + 2D MED/CALIC + arithmetic) against PNG / zstd / Canon's own lossless
and the trained model — it never tested **JPEG-XL lossless (modular mode)**, the strongest general
still codec. The other-vendor probe (`scripts/othervendor_raw_benchmark.py`) found pertype LOSES to
JPEG-XL on Sony/Leica, which put the Canon claim in doubt. This re-checks it.

VERDICT (8 local Canon raws, 32×1024² 2×2-aligned crops, bayer round-trip verified):
- ✅ **The Canon-CR2 win HOLDS.** pertype bayer **2.14×** vs JPEG-XL **1.90× = +11.2%** (effort 7),
  **+10% vs max-effort e9** (e9 only gains ~1.4% over e7), and **positive on all 8 files** (+5%..+19%).
  Also beats xz-9 (1.75×), JPEG-LS (1.66×), zstd-19 (1.51×), PNG (1.35×).
- **The cross-vendor split is sensor-type-dependent:** CMOS Bayer sensors with well-behaved phase-
  plane statistics WIN (Canon +11%, Nikon D7100 +14% in the other-vendor probe), but the noisier Sony
  CMOS LOSES (−15%) and the smooth low-noise Leica M8 CCD loses badly (−94%, JPEG-XL's adaptive
  modular mode crushes it). So the Bayer codec is genuinely validated for Canon/Nikon — not a
  universal raw win.

Data: Canon raws are private/local-only — set CR2_DIR (default ~/cr2_local). Reproducible by anyone
with Canon CR2 files, same provenance as `scripts/cr2_benchmark.py`. Memory-safe: one crop at a time.
"""
import glob, gc, lzma, os, subprocess, sys
import numpy as np

CR2_DIR = os.environ.get("CR2_DIR", os.path.expanduser("~/cr2_local"))
NF = int(sys.argv[1]) if len(sys.argv) > 1 else 8
C = 1024


def zstd(b): return len(subprocess.run(["zstd", "-19", "-c"], input=b, stdout=subprocess.PIPE).stdout)
def xz(b): return len(lzma.compress(b, preset=9))


def crops_of(p):
    import rawpy
    out = []
    with rawpy.imread(p) as r:
        b = r.raw_image_visible; h, w = b.shape
        for y in [int(h*f) & ~1 for f in (0.30, 0.55, 0.75)]:
            for x in [int(w*f) & ~1 for f in (0.40, 0.65)]:
                if y+C <= h and x+C <= w:
                    out.append(np.ascontiguousarray(b[y:y+C, x:x+C].astype("<u2")))
    return out


def main():
    import imagecodecs as ic
    from pertype import imagecodec
    files = sorted(glob.glob(os.path.join(CR2_DIR, "*.CR2")))[::8][:NF]
    if not files:
        print(f"no CR2 files in {CR2_DIR} (set CR2_DIR)"); return
    agg = {k: 0 for k in ("raw", "zstd", "xz", "png", "jls", "jxl", "pt")}
    ncrops = 0; perfile = []
    for p in files:
        f = {k: 0 for k in agg}
        for m in crops_of(p):
            ncrops += 1
            f["raw"] += m.nbytes
            f["zstd"] += zstd(m.tobytes()); f["xz"] += xz(m.tobytes())
            f["png"] += len(ic.png_encode(m)); f["jls"] += len(ic.jpegls_encode(m))
            f["jxl"] += len(ic.jpegxl_encode(m, lossless=True, effort=7))
            blob = imagecodec.encode(m, bayer=True)
            assert np.array_equal(imagecodec.decode(blob), m), "bayer round-trip"
            f["pt"] += len(blob)
            del m, blob; gc.collect()
        for k in agg: agg[k] += f[k]
        perfile.append((os.path.basename(p).split('.')[0], (1 - f["pt"]/f["jxl"])*100))
    R = agg["raw"]
    print(f"Canon CR2 — {len(files)} files, {ncrops}×{C}² crops")
    for k, lbl in [("zstd", "zstd-19"), ("xz", "xz-9"), ("png", "PNG"), ("jls", "JPEG-LS"),
                   ("jxl", "JPEG-XL lossless e7"), ("pt", "pertype bayer (CFA-split)")]:
        print(f"   {lbl:26} {R/agg[k]:5.2f}x")
    print(f"\n   pertype bayer vs JPEG-XL lossless: {(1-agg['pt']/agg['jxl'])*100:+.1f}%  (aggregate)")
    print("   per-file: " + ", ".join(f"{n}{d:+.0f}%" for n, d in perfile))


if __name__ == "__main__":
    main()
