"""Measure-first: multiplexed spatial-omics imaging (real CyCIF multiplexed-IF, uint16) — Tier-2.

Premise: a multiplexed tissue image is N co-registered fluorescence channels (antibody/probe
stains) over the same field — seemingly the multispectral shape (per-channel 2D spatial structure)
PLUS a seductive *inter-channel* lever (all channels see the same tissue, the same nuclei). The
hope was pertype's existing 2D image codec (MED/CALIC predictor + `ctxcoder`, the multispectral /
depth winner) per channel, and the volume path to exploit inter-channel redundancy.

VERDICT (real data: MCMICRO `exemplar-001`, a CyCIF lung-adenocarcinoma TMA core, 3 cycles × 4
channels = 12 co-registered uint16 planes 1080×1280; per-channel + volume round-trip verified):
- ⚠️ **CONDITIONAL — marginal win.** Per-channel 2D `imagecodec` = **1.85×**, which **beats the
  field's actual storage** (OME-Zarr/OME-TIFF Blosc byte-shuffle+zstd **1.61×, +13.1%**) but only
  **thinly edges the SOTA still specialists**: vs **JPEG-XL lossless (effort 9) +1.8%**, vs
  **JPEG-LS +2.2%**. The +1.8% over JPEG-XL is robust (max-effort JPEG-XL gains <0.1%), and it is
  *positive* across all 12 channels — a genuine but small edge for the MED/CALIC + context-
  arithmetic coder over JPEG-XL, not a double-digit win like multispectral (+48%) or depth (+23%).
- **The inter-channel lever is DISCONFIRMED: −8.9%** (`encode_volume` on the 4 exactly-co-registered
  channels per cycle vs the per-channel sum). Co-registered multiplexed channels stain *different*
  structures (DAPI nuclei vs membrane/cytoplasm antibody markers), so they are spatially
  decorrelated — inter-channel prediction *inflates*. Same shape as multispectral inter-band
  (bands too far apart), ephys cross-channel, and MRI multi-coil.
- **The one genuine inter-channel redundancy is off-limits to lossless:** CyCIF re-images the *same*
  DAPI nuclear stain every cycle (channels 1/5/9 here), so those planes ARE near-duplicate — but
  the raw cycles are *pre-registration* (cycle-to-cycle stage drift), so the duplication only exists
  after a lossy alignment (registration). Reaching it throws bits away — meta-lesson #5 (MRI
  image-space redundancy) recurs exactly.
- **Net:** coverage of the validated per-channel 2D-spatial win into a new domain (beats the field's
  Blosc storage solidly, edges even JPEG-XL), reusing the existing codec with zero new code — but
  NOT a clean win over the SOTA still bar and NOT a new lever. Honest call: ⚠️, leaning positive.

Data: 3 public CyCIF OME-TIFFs from the MCMICRO `exemplar-001` S3 bucket (no login, ~64 MB each).
"""
import lzma, os, subprocess, sys, urllib.request
import numpy as np

BASE = "https://mcmicro.s3.amazonaws.com/exemplars/001/exemplar-001/raw/exemplar-001-cycle-{}.ome.tiff"
CY = ["06", "07", "08"]
DDIR = sys.argv[1] if len(sys.argv) > 1 else "spatialomics_data"


def fetch():
    os.makedirs(DDIR, exist_ok=True)
    out = []
    for c in CY:
        p = os.path.join(DDIR, f"cycle-{c}.ome.tiff")
        if not os.path.exists(p):
            urllib.request.urlretrieve(BASE.format(c), p)
        out.append(p)
    return out


def zstd(b, l=19): return len(subprocess.run(["zstd", f"-{l}", "-c"], input=b, stdout=subprocess.PIPE).stdout)
def xz(b): return len(lzma.compress(b, preset=9))
def shuf_zstd(b, w=2): return zstd(np.frombuffer(b, np.uint8).reshape(-1, w).T.tobytes())  # Blosc shuffle proxy


def main():
    import tifffile, imagecodecs as ic
    from pertype import imagecodec
    files = fetch()
    cubes = [tifffile.imread(f) for f in files]                 # each (4,H,W) uint16
    planes = [cubes[k][i] for k in range(len(cubes)) for i in range(cubes[k].shape[0])]
    H, W = planes[0].shape
    raw = sum(p.nbytes for p in planes)
    print(f"{len(planes)} channels {H}x{W} uint16, raw {raw/1e6:.1f} MB")

    def per(fn): return sum(fn(p) for p in planes)
    bars = {
        "zstd-19 (plane-major)":      per(lambda p: zstd(p.tobytes())),
        "byte-shuffle+zstd (Blosc)":  per(lambda p: shuf_zstd(p.tobytes())),
        "xz-9":                       per(lambda p: xz(p.tobytes())),
        "PNG":                        per(lambda p: len(ic.png_encode(p))),
        "JPEG-LS":                    per(lambda p: len(ic.jpegls_encode(p))),
        "JPEG-XL lossless (e9)":      per(lambda p: len(ic.jpegxl_encode(p, lossless=True, effort=9))),
    }
    pc = 0
    for p in planes:
        blob = imagecodec.encode(p, bayer=False)
        assert np.array_equal(imagecodec.decode(blob), p), "imagecodec round-trip"
        pc += len(blob)
    vol = 0
    for cube in cubes:
        v = imagecodec.encode_volume(cube)
        assert np.array_equal(imagecodec.decode_volume(v), cube), "volume round-trip"
        vol += len(v)

    best = min(bars["JPEG-XL lossless (e9)"], bars["JPEG-LS"], bars["byte-shuffle+zstd (Blosc)"])
    print("\n  codec                          ratio    MB    vs best-bar")
    for k, b in bars.items():
        print(f"   {k:29} {raw/b:5.2f}x  {b/1e6:5.2f}  {(1-b/best)*100:+6.1f}%")
    for k, b in [("pertype per-ch 2D imagecodec", pc), ("pertype inter-ch volume", vol)]:
        print(f"   {k:29} {raw/b:5.2f}x  {b/1e6:5.2f}  {(1-b/best)*100:+6.1f}%")
    print(f"\n  per-ch 2D vs field Blosc:  {(1-pc/bars['byte-shuffle+zstd (Blosc)'])*100:+.1f}%")
    print(f"  per-ch 2D vs SOTA JPEG-XL: {(1-pc/bars['JPEG-XL lossless (e9)'])*100:+.1f}%")
    print(f"  inter-channel lever (volume vs per-ch sum): {(1-vol/pc)*100:+.1f}%  (negative = HURTS)")


if __name__ == "__main__":
    main()
