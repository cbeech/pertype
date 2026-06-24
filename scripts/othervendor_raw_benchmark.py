"""Measure-first: other-vendor raw Bayer mosaics (Sony ARW / Nikon NEF / Leica DNG) — Tier-2.

Premise: pertype already has a validated Canon-CR2 Bayer codec (`imagecodec`, bayer=True: split the
CFA mosaic into its 4 phase planes R/G1/G2/B, then 2D MED/CALIC-predict + arithmetic-code each).
The hope was free *coverage* — the same codec should beat the field across vendors/CFA layouts/bit
depths. The honest bar is the strongest lossless still codec, **JPEG-XL lossless (modular mode)**,
which Adobe DNG 1.7 now even adopts — NOT the PNG/zstd the original CR2 benchmark used.

VERDICT (real public CC0 raws from rawsamples.ch — Sony ILCE-7RM2 ARW, Nikon D7100 NEF, Leica M8
DNG; full sensor mosaics via rawpy/LibRaw; several 1024² 2×2-aligned crops/image, processed one at
a time for bounded memory; bayer round-trip verified):
- ❌ **RULED OUT as a clean coverage win.** Against JPEG-XL lossless the result is **mixed and
  sensor-dependent**, not a transfer of the Canon win:
  - Nikon D7100:  pertype 2.17× vs JPEG-XL 1.86×  → **+14.3%**  (pertype wins)
  - Sony A7RII:   pertype 1.83× vs JPEG-XL 2.10×  → **−14.5%**  (pertype loses)
  - Leica M8:     pertype 2.67× vs JPEG-XL 5.19×  → **−94%**    (smooth low-noise CCD; JPEG-XL crushes it)
  pertype's CFA-split codec *does* broadly beat zstd-19 / byte-shuffle+zstd / xz-9 / PNG / JPEG-LS,
  but JPEG-XL's modular lossless mode (its own strong context model, and it handles the 2×2 mosaic
  structure well even without being told it is Bayer) wins on 2 of 3 sensors. An oracle per-image
  router is just "use JPEG-XL except on Nikon" — i.e. JPEG-XL is the better default; the CFA-split
  lever only sometimes helps. Not a win.
- **Why it diverges by sensor:** the CFA-phase split + fixed MED predictor pays off only where each
  phase plane is smooth enough for a horizontal/MED predictor to beat JPEG-XL's adaptive modular
  transforms (Nikon). On noisier (Sony) or very smooth (Leica CCD) data JPEG-XL's per-image adaptive
  modelling wins. There is no single sensor-agnostic lever — meta-lesson #3 again: beating PNG/zstd
  (the original CR2 bar) is **not** beating the strongest real specialist.
- **Caveat / follow-up:** this also calls the strength-of-bar of the existing Canon-CR2 claim into
  question — that win was measured vs PNG/zstd/Canon-lossless and the trained pertype model, **never
  vs JPEG-XL lossless**. If JPEG-XL beats the Bayer codec on most other-vendor sensors, it may beat
  it on Canon too; worth re-benchmarking the CR2 entry against JPEG-XL before relying on it.

Data: 3 public CC0 sample raws fetched over HTTPS from rawsamples.ch (no login). Memory-safe: never
holds a full 42 MP frame through a codec — slices small crops inside the rawpy block and frees each.
"""
import gc, lzma, os, subprocess, sys, urllib.request
import numpy as np

FILES = [
    ("https://www.rawsamples.ch/raws/sony/RAW_SONY_ILCE-7RM2.ARW", "sony_a7rii.ARW"),
    ("https://www.rawsamples.ch/raws/nikon/RAW_NIKON_D7100.NEF",   "nikon_d7100.NEF"),
    ("https://www.rawsamples.ch/raws/leica/m8/RAW_LEICA_M8.DNG",   "leica_m8.DNG"),
]
DDIR = sys.argv[1] if len(sys.argv) > 1 else "raw_data"
C = 1024  # crop side (1 MP) — bounded memory


def fetch():
    os.makedirs(DDIR, exist_ok=True)
    out = []
    for url, name in FILES:
        p = os.path.join(DDIR, name)
        if not os.path.exists(p):
            urllib.request.urlretrieve(url, p)
        out.append(p)
    return out


def zstd(b, l=19): return len(subprocess.run(["zstd", f"-{l}", "-c"], input=b, stdout=subprocess.PIPE).stdout)
def xz(b): return len(lzma.compress(b, preset=9))
def shuf_zstd(b): return zstd(np.frombuffer(b, np.uint8).reshape(-1, 2).T.tobytes())


def crops_of(p):
    import rawpy
    out = []
    with rawpy.imread(p) as r:
        b = r.raw_image_visible
        h, w = b.shape
        for y in [int(h*f) & ~1 for f in (0.25, 0.50, 0.75)]:
            for x in [int(w*f) & ~1 for f in (0.33, 0.66)]:
                if y+C <= h and x+C <= w:
                    out.append(np.ascontiguousarray(b[y:y+C, x:x+C].astype("<u2")))
    return out, h, w, os.path.getsize(p)


def main():
    import imagecodecs as ic
    from pertype import imagecodec
    for p in fetch():
        crops, h, w, fsz = crops_of(p)
        cam = h * w * 2 / fsz
        agg = {k: 0 for k in ("raw", "zstd", "shuf", "xz", "jls", "jxl", "pt")}
        for m in crops:
            agg["raw"]  += m.nbytes
            agg["zstd"] += zstd(m.tobytes())
            agg["shuf"] += shuf_zstd(m.tobytes())
            agg["xz"]   += xz(m.tobytes())
            agg["jls"]  += len(ic.jpegls_encode(m))
            agg["jxl"]  += len(ic.jpegxl_encode(m, lossless=True, effort=7))
            blob = imagecodec.encode(m, bayer=True)
            assert np.array_equal(imagecodec.decode(blob), m), "bayer round-trip"
            agg["pt"]   += len(blob)
            del m, blob; gc.collect()
        R = agg["raw"]; best = min(agg["jxl"], agg["jls"])
        print(f"\n{os.path.basename(p):18} {len(crops)}×{C}² crops  [in-camera lossless full-frame {cam:.2f}x]")
        for k, lbl in [("zstd", "zstd-19"), ("shuf", "byte-shuffle+zstd"), ("xz", "xz-9"),
                       ("jls", "JPEG-LS"), ("jxl", "JPEG-XL lossless e7"), ("pt", "pertype bayer (CFA-split)")]:
            print(f"   {lbl:26} {R/agg[k]:5.2f}x  {(1-agg[k]/best)*100:+6.1f}% vs best-still")
        print(f"   -> pertype bayer vs JPEG-XL lossless: {(1-agg['pt']/agg['jxl'])*100:+.1f}%")


if __name__ == "__main__":
    main()
