"""Measure-first: 16-bit radiometric thermal IR (target — watch-list "Thermal/radiometric IR").

Premise: thermal radiometric frames (FLIR uncooled microbolometer, raw pre-AGC counts) are
smooth 16-bit imagery + temporal video. The field stores them per-frame as 16-bit PNG/TIFF
(LTIR ships exactly this); the specialist lossless still bar is JPEG-LS (Golomb). Question:
does pertype's 2D MED/CALIC + arithmetic beat those, and does inter-frame delta help on the
video sequences?

VERDICT (real data: LTIR v1.0, 6 sixteen-bit radiometric FLIR sequences, 24 frames each —
genuinely pre-AGC: 13–15 bits used, std up to 1100 counts):
- **Spatial (per-frame) — NOT a standalone win, only TIES the specialist bar.** pertype's 2D
  codec is **+2.2% vs JPEG-LS / +1.3% vs JPEG-XL-lossless** — within noise. It beats *PNG* by
  +29%, but that only says PNG (Paeth+DEFLATE) is a weak bar for 16-bit thermal; JPEG-LS and
  JXL already sit where pertype's predictor sits. As a still codec, thermal is a tie.
- **Temporal (inter-frame delta) — the REAL lever: +31.4% vs JPEG-LS / +30.8% vs JPEG-XL-ll.**
  `encode_volume` codes frame 0 directly then each later frame as its delta from the previous;
  the specialist still bars code every frame independently and have NO temporal model, so the
  static-background redundancy of fixed-camera thermal video is pure profit (3.65× vs 2.50×).
  **Content-dependent:** wins big on static-background 480×640 sequences (car +37.6%,
  quadrocopter +38.7%, crouching +31.3%, trees +28.5%) — the dominant real deployment (fixed
  monitoring/surveillance/fever-screening cameras) — but is flat-to-negative on the small
  high-motion clips (garden −2.4%, horse +4.0%).
- Net: ⚠️ **CONDITIONAL WIN — temporal, not spatial.** Free (reuses `encode_volume`, no
  thermal-specific code), real, and large on the right content. Caveat: the +31% is vs
  frame-independent bars (JPEG-LS/JXL/PNG/TIFF — what radiometric IR actually ships as, since
  16-bit radiometric needs frame-exact counts); an inter-frame lossless *video* codec
  (FFV1 is intra-only, but lossless x265) would also capture the background and shrink the
  margin — untested. Bit depth 13–15; HOA-style >2 sensors / cooled-sensor data untested.

Data: LTIR v1.0 (Linköping Thermal InfraRed), the 16-bit sequences. The full archive is 3.9 GB,
so this reads it as a REMOTE partial ZIP — parse the central directory from the tail, then
range-fetch only the frames we need (same trick as the safetensors/LLM-VRAM probe). No login.
"""
import io
import struct
import sys
import urllib.request
import zlib

import numpy as np
from PIL import Image

from pertype import imagecodec as ic

URL = ("https://www.cvl.isy.liu.se/research/datasets/ltir/version1.0/"
       "ltir_v1_0_8bit_16bit.zip")
ROOT = "ltir_v1_0_8bit_16bit"
SEQS = ["16_car", "16_quadrocopter2", "16_horse", "16_trees", "16_garden", "16_crouching"]
NFRAMES = 24


# --- remote partial-ZIP reader (archive <4 GB -> classic, non-ZIP64 EOCD) -----------
def _get(a, b):
    r = urllib.request.Request(URL, headers={"Range": f"bytes={a}-{b}"})
    return urllib.request.urlopen(r).read()


def central_dir():
    n = int(urllib.request.urlopen(urllib.request.Request(URL, method="HEAD"))
            .headers["Content-Length"])
    tail = _get(n - 65557, n - 1)
    eocd = tail[tail.rfind(b"PK\x05\x06"):][:22]
    _, _, _, _, _, cd_size, cd_off, _ = struct.unpack("<IHHHHIIH", eocd)
    cd = _get(cd_off, cd_off + cd_size - 1)
    entries, p = [], 0
    while p < len(cd) and cd[p:p + 4] == b"PK\x01\x02":
        (_, _, _, _, method, _, _, _, csize, _,
         nlen, elen, clen, _, _, _, lho) = struct.unpack("<IHHHHHHIIIHHHHHII", cd[p:p + 46])
        name = cd[p + 46:p + 46 + nlen].decode("utf-8", "replace")
        entries.append(dict(name=name, method=method, csize=csize, lho=lho))
        p += 46 + nlen + elen + clen
    return entries


def fetch(e):
    lh = _get(e["lho"], e["lho"] + 29)
    _, _, _, _, _, _, _, _, _, nlen, elen = struct.unpack("<IHHHHHIIIHH", lh)
    start = e["lho"] + 30 + nlen + elen
    blob = _get(start, start + e["csize"] - 1)
    return zlib.decompress(blob, -15) if e["method"] == 8 else blob


def load(entries, seq):
    es = sorted((e for e in entries if e["name"].startswith(f"{ROOT}/{seq}/")
                 and e["name"].lower().endswith(".png")), key=lambda e: e["name"])[:NFRAMES]
    return np.stack([np.asarray(Image.open(io.BytesIO(fetch(e)))) for e in es])


# --- bars ---------------------------------------------------------------------------
def png9(a):
    buf = io.BytesIO()
    Image.fromarray(a).save(buf, format="PNG", optimize=True, compress_level=9)
    return len(buf.getbuffer())


def main():
    try:
        import imagecodecs
        have_ic = True
    except ImportError:
        have_ic = False
        print("(imagecodecs not installed — skipping JPEG-LS / JPEG-XL bars)")

    entries = central_dir()
    print(f"{'seq':18} {'shape':16} bits  std | {'raw':>7} {'PNG9':>6} "
          f"{'JLS':>6} {'pt2D':>6} {'ptVol':>6} | 2D/JLS  Vol/JLS")
    T = dict(raw=0, png9=0, jls=0, jxl=0, pt=0, vol=0)
    for seq in SEQS:
        vol = load(entries, seq)
        N = vol.shape[0]
        assert vol.dtype == np.uint16
        bits = int(np.ceil(np.log2(int(vol.max()) + 1)))
        raw = vol.size * 2
        p9 = sum(png9(vol[i]) for i in range(N))
        pt = sum(len(ic.encode(vol[i], bayer=False)) for i in range(N))
        ptv = len(ic.encode_volume(vol))
        # round-trip check
        assert (ic.decode_volume(ic.encode_volume(vol)) == vol).all()
        jls = sum(len(imagecodecs.jpegls_encode(vol[i])) for i in range(N)) if have_ic else 0
        jxl = (sum(len(imagecodecs.jpegxl_encode(vol[i], lossless=True)) for i in range(N))
               if have_ic else 0)
        for k, v in [("raw", raw), ("png9", p9), ("jls", jls), ("jxl", jxl),
                     ("pt", pt), ("vol", ptv)]:
            T[k] += v
        jr = f"{(1-pt/jls)*100:+5.1f}% {(1-ptv/jls)*100:+6.1f}%" if jls else "   (no JLS)"
        print(f"{seq:18} {str(vol.shape):16} {bits:4d} {vol.std():5.0f} | "
              f"{raw/1e6:6.2f}M {p9/1e6:5.2f}M {jls/1e6:5.2f}M {pt/1e6:5.2f}M {ptv/1e6:5.2f}M | {jr}")

    print("-" * 96)
    print(f"raw {T['raw']/1e6:.1f}M | PNG9 {T['raw']/T['png9']:.2f}x  "
          + (f"JPEG-LS {T['raw']/T['jls']:.2f}x  JXL-ll {T['raw']/T['jxl']:.2f}x  " if T['jls'] else "")
          + f"pt2D {T['raw']/T['pt']:.2f}x  ptVol {T['raw']/T['vol']:.2f}x")
    print(f"pertype 2D  vs PNG9: {(1-T['pt']/T['png9'])*100:+.1f}%"
          + (f"   vs JPEG-LS: {(1-T['pt']/T['jls'])*100:+.1f}%   vs JXL-ll: {(1-T['pt']/T['jxl'])*100:+.1f}%" if T['jls'] else ""))
    print(f"pertype Vol vs PNG9: {(1-T['vol']/T['png9'])*100:+.1f}%"
          + (f"   vs JPEG-LS: {(1-T['vol']/T['jls'])*100:+.1f}%   vs JXL-ll: {(1-T['vol']/T['jxl'])*100:+.1f}%" if T['jls'] else "")
          + f"   (Vol vs 2D: {(1-T['vol']/T['pt'])*100:+.1f}%)")
    print("=> spatial ties the JPEG-LS/JXL specialist bar; the temporal inter-frame delta is "
          "the real (content-dependent) win.")


if __name__ == "__main__":
    main()
