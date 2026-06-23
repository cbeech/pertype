"""Measure-first: mass-spec proteomics — mzML m/z + intensity arrays (target #9).

Premise: a mass spectrum stores two parallel arrays per scan — m/z (sorted, generated on
the instrument's near-linear/√ calibration grid → second differences ≈ 0) and intensity
(spiky positive floats). The field stores each binary array independently: standard mzML
base64+**zlib**; the lossless storage specialist is **mzMLb** = HDF5 with **byte-shuffle+zlib**.
Question: does pertype's columnar (byte de-interleave + per-column Δ/Δ², the financial/CAN
timestamp lever) beat them on the near-linear m/z grid?

VERDICT (real data: PRIDE PXD000001, 145 Orbitrap profile-mode spectra, float64; ~25k
peaks/spectrum; per-spectrum/per-array — the real compression unit):
- **m/z array — a clean WIN.** pertype `columnar` (width-8): **2.08× = +35.4% vs zlib
  (mzML standard) and +17.9% vs the byte-shuffle/HDF5 specialist (mzMLb-style).** The
  near-linear sorted float64 grid is exactly the columnar Δ² lever — de-interleave the 8
  byte-planes, and each plane's per-column Δ/Δ² collapses (same mechanism as financial ns
  timestamps / CAN-bus). `floatcodec` only ties zlib (m/z is all-distinct → the distinct-
  value dictionary path no-ops).
- **intensity array — NOT a win.** Spiky high-entropy float mantissa: xz (4.57×) and
  shuffle (4.43×) win; pertype columnar **−15% vs zlib** (Δ on spiky data inflates), floatcodec
  −9.5%. Route intensity to shuffle/xz, not columnar.
- **Net (per-array routing — legitimate: mzML tags each array's type):** m/z→columnar,
  intensity→xz gives **2.86× combined = +30.3% vs zlib (mzML standard), +13.8% vs the
  HDF5-shuffle specialist.** Real win, driven entirely by the m/z columnar lever.
- ⚠️ Caveats: one dataset (Orbitrap, float64, profile mode). **Untested:** float32 m/z
  (common; byte-planes differ), centroided peak lists (fewer peaks, less-smooth grid → the
  Δ² lever may weaken), other vendors. MS-Numpress is the named specialist but it is
  fixed-precision/**lossy**, so excluded from this lossless comparison (mzMLb/shuffle is the
  lossless storage bar, which we beat).

Data: streamed as a remote partial read — range-fetch a ~40 MB prefix of a 450 MB PRIDE
mzML, take complete <spectrum> blocks, decode each binaryDataArray (base64 → zlib →
float64). No download/login.
"""
import base64
import lzma
import re
import subprocess
import sys
import urllib.request
import zlib

import numpy as np

from pertype import floatcodec, columnar

URL = ("https://ftp.pride.ebi.ac.uk/pride/data/archive/2012/03/PXD000001/"
       "TMT_Erwinia_1uLSike_Top10HCD_isol2_45stepped_60min_01-20141210.mzML")
PREFIX = int(sys.argv[1]) if len(sys.argv) > 1 else 40_000_000

SPEC = re.compile(rb"<spectrum\b.*?</spectrum>", re.S)
BDA = re.compile(rb"<binaryDataArray\b.*?</binaryDataArray>", re.S)
BIN = re.compile(rb"<binary>(.*?)</binary>", re.S)


def fetch_prefix(nbytes):
    r = urllib.request.Request(URL, headers={"Range": f"bytes=0-{nbytes - 1}"})
    return urllib.request.urlopen(r).read()


def _decode_bda(block):
    is64 = b"MS:1000523" in block
    zipped = b"MS:1000574" in block
    kind = "mz" if b"MS:1000514" in block else ("int" if b"MS:1000515" in block else "?")
    m = BIN.search(block)
    raw = base64.b64decode(m.group(1)) if m and m.group(1).strip() else b""
    if zipped and raw:
        raw = zlib.decompress(raw)
    return kind, np.frombuffer(raw, "<f8" if is64 else "<f4")


def parse(data):
    out = []
    for sm in SPEC.finditer(data):
        arrs = {}
        for bm in BDA.finditer(sm.group(0)):
            k, a = _decode_bda(bm.group(0))
            if k in ("mz", "int"):
                arrs[k] = a
        if "mz" in arrs and "int" in arrs and len(arrs["mz"]):
            out.append(arrs)
    return out


def zstd(b, lvl=19):
    return subprocess.run(["zstd", f"-{lvl}", "-c"], input=b, stdout=subprocess.PIPE).stdout


def shuffle_zstd(arr):
    u8 = arr.view(np.uint8).reshape(-1, arr.dtype.itemsize)
    return zstd(np.ascontiguousarray(u8.T).tobytes())


def code_array(arr, acc):
    b = arr.tobytes()
    isz = arr.dtype.itemsize
    acc["raw"] += len(b)
    acc["zlib"] += len(zlib.compress(b, 9))
    acc["xz"] += len(lzma.compress(b, preset=9))
    acc["shuf"] += len(shuffle_zstd(arr))
    acc["flt"] += len(floatcodec.encode(b, isz))
    col = columnar.encode(b, width=isz)
    acc["col"] += len(col)
    # round-trip the pertype paths (lossless guarantee)
    assert floatcodec.decode(floatcodec.encode(b, isz)) == b
    assert columnar.decode(col) == b


def main():
    data = fetch_prefix(PREFIX)
    data = data[:data.rfind(b"</spectrum>") + len(b"</spectrum>")]
    specs = parse(data)
    keys = ["raw", "zlib", "xz", "shuf", "flt", "col"]
    mz = {k: 0 for k in keys}
    it = {k: 0 for k in keys}
    for s in specs:
        code_array(s["mz"].astype("<f8"), mz)
        code_array(s["int"].astype("<f8"), it)
    print(f"{len(specs)} spectra, float64 (round-trip verified)\n")
    for name, acc in [("m/z", mz), ("intensity", it)]:
        r = acc["raw"]
        print(f"== {name} array ==  raw {r/1e6:.1f} MB")
        for k in keys[1:]:
            print(f"   {k:5} {r/acc[k]:6.2f}x   vs zlib {(1-acc[k]/acc['zlib'])*100:+6.1f}%"
                  f"   vs shuf(mzMLb) {(1-acc[k]/acc['shuf'])*100:+6.1f}%")
        print()
    # honest design: per-array routing (mzML tags each array's type)
    best_mz, best_it = mz["col"], it["xz"]              # m/z->columnar, intensity->xz
    route = best_mz + best_it
    zl = mz["zlib"] + it["zlib"]
    sh = mz["shuf"] + it["shuf"]
    print("== per-array routing (m/z→columnar, intensity→xz) ==")
    print(f"   {(mz['raw']+it['raw'])/route:.2f}x combined   "
          f"+{(1-route/zl)*100:.1f}% vs zlib (mzML std)   "
          f"+{(1-route/sh)*100:.1f}% vs byte-shuffle (mzMLb/HDF5)")


if __name__ == "__main__":
    main()
