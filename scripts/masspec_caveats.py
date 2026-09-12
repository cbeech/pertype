"""Measure-first: close the two documented caveats in the mass-spec sweep verdict.

The committed verdict (masspec_benchmark.py, PRIDE PXD000001, 145 Orbitrap profile
spectra, float64): m/z array is a clean WIN for pertype `columnar` (+35.4% vs zlib,
+17.9% vs byte-shuffle/HDF5), intensity is NOT (xz wins) -> per-array routing.
Two caveats were never tested:

  1. float32 m/z — most mzML stores float32; downcasting float64->float32 changes the
     byte grid (4 byte-planes, not 8). Does the per-column Δ² lever survive, and does
     the win survive against bars recomputed on the same float32 bytes?
  2. Centroided peak lists — m/z quantized to discrete peak centers, less-smooth grid,
     the Δ² lever may weaken.

Env:
  MASSPEC_PREFIX  bytes of PXD000001 to range-fetch (default 40_000_000, same as original)
  CENTROID_URL    public centroided .mzML(.gz) (PRIDE archive) for caveat 2; unset/failed
                  download/undecodable -> experiment reported BLOCKED with evidence
  CENTROID_PATH   local cache for that download (default /tmp/masspec_caveat_centroided.mzML)

Bars per array byte-form: zlib-1 (fast mzML-writer bar), zlib-9, xz -9,
byte-shuffle+zstd-19 (mzMLb/HDF5 specialist). pertype `columnar` on the same bytes,
round-trip verified. float32 vs float64 is compared honestly: every bar is recomputed
on the same-form bytes and ratios are taken against the same-form zlib bar.

Data budget: PXD000001 prefix (40 MB) + one <=25 MB centroided mzML.
"""
import lzma
import os
import sys
import urllib.request
import zlib

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from masspec_benchmark import SPEC, BIN, URL, fetch_prefix, zstd, shuffle_zstd  # noqa: E402
from pertype import columnar  # noqa: E402

PREFIX = int(os.environ.get("MASSPEC_PREFIX", 40_000_000))
CENTROID_URL = os.environ.get("CENTROID_URL", "")
CENTROID_PATH = os.environ.get("CENTROID_PATH", "/tmp/masspec_caveat_centroided.mzML")

BARS = ["zlib1", "zlib9", "xz", "shuf", "col"]
NUMPRESS = (b"MS:1000572", b"MS:1000573")  # numpress-compressed arrays we cannot decode


def code_array(arr, acc):
    """Measure every bar on one array's exact bytes; round-trip the pertype path."""
    b = arr.tobytes()
    isz = arr.dtype.itemsize
    acc["raw"] += len(b)
    acc["zlib1"] += len(zlib.compress(b, 1))
    acc["zlib9"] += len(zlib.compress(b, 9))
    acc["xz"] += len(lzma.compress(b, preset=9))
    acc["shuf"] += len(shuffle_zstd(arr))
    col = columnar.encode(b, width=isz)
    acc["col"] += len(col)
    assert columnar.decode(col) == b  # lossless guarantee


def report(name, acc, n):
    r = acc["raw"]
    print(f"== {name} ==  raw {r/1e6:.1f} MB  ({n} arrays)")
    for k in BARS:
        print(f"   {k:5} {r/acc[k]:6.2f}x   vs zlib {(1-acc[k]/acc['zlib1'])*100:+6.1f}%"
              f"   vs shuf(mzMLb) {(1-acc[k]/acc['shuf'])*100:+6.1f}%")
    print()


def verdict(win_name, acc):
    v_z = (1 - acc["col"] / acc["zlib1"]) * 100
    v_s = (1 - acc["col"] / acc["shuf"]) * 100
    print(f"VERDICT {win_name}: columnar {v_z:+.1f}% vs zlib, {v_s:+.1f}% vs shuffle"
          f" -> {'WIN — lever survives' if v_z > 0 and v_s > 0 else 'NO WIN — lever weakened'}")
    print()


def exp_float32():
    """Caveat 1: same PXD000001 spectra, m/z measured as float64 and downcast float32."""
    print(f"### Caveat 1 — float32 m/z  ({URL}, prefix {PREFIX/1e6:.0f} MB)")
    data = fetch_prefix(PREFIX)
    data = data[:data.rfind(b"</spectrum>") + len(b"</spectrum>")]
    specs = parse_profile(data)
    f64 = {k: 0 for k in ["raw"] + BARS}
    f32 = {k: 0 for k in ["raw"] + BARS}
    for s in specs:
        mz64 = np.ascontiguousarray(s["mz"])
        assert mz64.dtype == np.float64, "PXD000001 m/z must decode as float64"
        code_array(mz64, f64)
        code_array(np.ascontiguousarray(mz64.astype("<f4")), f32)
    print(f"{len(specs)} spectra, m/z only (round-trip verified)\n")
    report("m/z float64", f64, len(specs))
    report("m/z float32", f32, len(specs))
    print(f"cross-form: columnar f32/columnar f64 = {f32['col']/f64['col']:.3f}x size; "
          f"win vs zlib bar f64 {(1-f64['col']/f64['zlib1'])*100:+.1f}% -> "
          f"f32 {(1-f32['col']/f32['zlib1'])*100:+.1f}%; "
          f"vs shuffle f64 {(1-f64['col']/f64['shuf'])*100:+.1f}% -> "
          f"f32 {(1-f32['col']/f32['shuf'])*100:+.1f}%\n")
    verdict("float32 m/z", f32)


def parse_profile(data):
    """Reuse the benchmark's parse; PXD000001 is zlib-only."""
    import masspec_benchmark as mb
    return mb.parse(data)


def _decode_bda_careful(block):
    """Decode one binaryDataArray; return (kind, array, skipped_numpress)."""
    import base64
    import masspec_benchmark as mb
    if any(t in block for t in NUMPRESS):
        return None, None, True
    m = BIN.search(block)
    raw = base64.b64decode(m.group(1)) if m and m.group(1).strip() else b""
    if b"MS:1000574" in block and raw:
        raw = zlib.decompress(raw)
    kind = "mz" if b"MS:1000514" in block else ("int" if b"MS:1000515" in block else "?")
    return kind, np.frombuffer(raw, "<f8" if b"MS:1000523" in block else "<f4"), False


def exp_centroided():
    """Caveat 2: a small public centroided mzML; same bars + columnar on its m/z."""
    print("### Caveat 2 — centroided peak lists")
    if not CENTROID_URL:
        print("BLOCKED: CENTROID_URL not set (no centroided source confirmed)\n")
        return
    print(f"source: {CENTROID_URL}")
    try:
        if not (os.path.exists(CENTROID_PATH) and os.path.getsize(CENTROID_PATH) > 0):
            urllib.request.urlretrieve(CENTROID_URL, CENTROID_PATH)
        data = open(CENTROID_PATH, "rb").read()
    except Exception as e:
        print(f"BLOCKED: download/read failed: {e!r}\n")
        return
    print(f"downloaded {len(data)/1e6:.1f} MB -> {CENTROID_PATH}")
    if data[:2] == b"\x1f\x8b":  # PRIDE serves many mzML as .mzML.gz
        import gzip
        data = gzip.decompress(data)
        print(f"gunzipped -> {len(data)/1e6:.1f} MB mzML")
    n_c = n_p = 0
    acc = {k: 0 for k in ["raw"] + BARS}
    n_arr = n_skip = 0
    peaks = []
    for sm in SPEC.finditer(data):
        block = sm.group(0)
        if b"MS:1000127" in block:
            n_c += 1
        elif b"MS:1000128" in block:
            n_p += 1
        got = {}
        for bm in block.split(b"<binaryDataArray")[1:]:
            sub = bm.split(b"</binaryDataArray>", 1)[0]
            kind, a, skipped = _decode_bda_careful(b"<binaryDataArray" + sub + b"</binaryDataArray>")
            if skipped:
                n_skip += 1
                continue
            if kind in ("mz", "int") and a is not None and len(a):
                got[kind] = a
        # measure only centroid-tagged spectra — the caveat is the centroided grid
        if b"MS:1000127" in block and "mz" in got:
            n_arr += 1
            peaks.append(len(got["mz"]))
            code_array(np.ascontiguousarray(got["mz"]), acc)
    print(f"spectra tagged centroid MS:1000127: {n_c}, profile MS:1000128: {n_p}, "
          f"numpress arrays skipped: {n_skip}")
    if n_arr < 10 or n_c == 0:
        print(f"BLOCKED: only {n_arr} usable centroid spectra (centroid={n_c}, "
              f"profile={n_p}, numpress-skipped={n_skip}) — not a usable centroided set\n")
        return
    pk = np.asarray(peaks)
    print(f"{n_arr} centroided spectra (of {n_c + n_p} total), m/z native dtype per file, "
          f"peaks/spectrum mean {pk.mean():.0f} median {np.median(pk):.0f} "
          f"min {pk.min()} max {pk.max()} (round-trip verified)\n")
    report(f"centroided m/z ({CENTROID_URL.rsplit('/',1)[-1]})", acc, n_arr)
    verdict("centroided m/z", acc)


def main():
    exp_float32()
    exp_centroided()


if __name__ == "__main__":
    main()
