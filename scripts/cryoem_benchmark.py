"""Measure-first: cryo-EM counting-mode movie frames (sparse near-binary).

Direct electron detectors in counting mode produce extremely sparse frames — ~90-95%
exact-zero pixels, the rest a few electrons (1/2/3...). Real depositions store these
gain-corrected as float32 (e.g. EMPIAR-10061, K2 beta-galactosidase movies: 7676×7420×38,
8.7 GB each), where the non-zero pixels take a few hundred distinct gain-corrected values.

The field stores+compresses that float32 with generic LZ (MRCZ-zstd) or TIFF+LZW, which
RLE-crush the zeros but never reach the value-distribution entropy floor (~0.15 B/px here
vs float32's 4 B/px). The count-aware model: losslessly map the few hundred distinct float
values to symbols (a <4 KB dictionary), making the frame a sparse small-integer image our
context-adaptive arithmetic coder (`pertype.ctxcoder`) codes near the floor. Note spatial
prediction (imagecodec MED) HURTS on sparse data — pure adaptive entropy coding wins.

Bar to beat: zstd / xz / gzip on the raw float32 frame (≈ MRCZ-zstd / TIFF+LZW). Round-trip
is verified byte-exact (symbols → dictionary → original float32).

Data: a gain-corrected float32 counting frame. Range-download a strip from EMPIAR-10061:
  U=https://ftp.ebi.ac.uk/empiar/world_availability/10061/data/Movies/EMD-2984_0000_frames.mrc
  curl -r 1024-$((1024+7676*4*1000-1)) "$U" -o strip.f32   # header is 1024 B; 1000 rows
  CRYO_DATA=strip.f32 CRYO_NX=7676 python scripts/cryoem_benchmark.py
"""
import os
import subprocess
import time

import numpy as np

from pertype import ctxcoder, imagecodec

DATA = os.environ.get("CRYO_DATA", "data/cryo/strip.f32")
NX = int(os.environ.get("CRYO_NX", "7676"))


def sh(cmd, data):
    t = time.time()
    out = subprocess.run(cmd, input=data, stdout=subprocess.PIPE).stdout
    return len(out), time.time() - t


def main():
    f = np.fromfile(DATA, dtype="<f4")
    rows = f.size // NX
    f = f[: rows * NX]
    raw = f.tobytes()
    n = len(raw)
    nz = float((f == 0).mean())
    print(f"frame: {NX}x{rows} float32 = {n / 1e6:.1f} MB   "
          f"zeros: {100 * nz:.1f}%   max: {f.max():.1f}\n")
    print(f"{'method':<26}{'size (MB)':>12}{'ratio':>9}{'B/px':>10}{'time s':>9}")

    def row(name, size, secs):
        print(f"{name:<26}{size / 1e6:>12.3f}{n / size:>9.2f}{size / f.size:>10.4f}{secs:>9.1f}")

    # bar: generic LZ on the float32 frame (≈ MRCZ-zstd / TIFF+LZW)
    row("gzip -9", *sh(["gzip", "-9"], raw))
    bar, bt = sh(["zstd", "-19", "-c"], raw); row("zstd -19  (BAR)", bar, bt)
    row("xz -9", *sh(["xz", "-9", "-c"], raw))

    # count-aware: distinct gain-corrected values -> symbols, then ctxcoder (no prediction)
    vals, inv = np.unique(f, return_inverse=True)
    inv = np.asarray(inv).ravel().astype(np.int64)
    dic = vals.astype("<f4").tobytes()
    t = time.time()
    blob = ctxcoder.encode(inv)
    dec = np.asarray(ctxcoder.decode(blob, inv.size)).astype(np.int64)
    assert np.array_equal(vals[dec].astype("<f4"), f), "ctx round-trip FAILED"
    ours = len(blob) + len(dic)
    row(f"ours (symbol+ctxcoder)", ours, time.time() - t)
    print(f"  ({len(vals)} distinct values, {len(dic)} B dictionary)")

    # for contrast: predictive image codec on the same symbol image (expected worse)
    t = time.time()
    e = imagecodec.encode(inv.reshape(rows, NX).astype(np.uint16), bayer=False)
    d2 = np.asarray(imagecodec.decode(e)).astype(np.int64).ravel()
    assert np.array_equal(vals[d2].astype("<f4"), f), "img round-trip FAILED"
    row("ours (symbol+imagecodec)", len(e) + len(dic), time.time() - t)

    gain = (bar - ours) / bar * 100
    print(f"\nours vs zstd-19: {bar / ours:.3f}x  ({gain:+.1f}% smaller)  "
          f"-> {'WIN' if gain >= 3 else 'below bar'} (bar = beat zstd-generic / MRCZ-zstd)")
    print("round-trip: OK (symbol -> dictionary -> float32, byte-exact)")


if __name__ == "__main__":
    main()
