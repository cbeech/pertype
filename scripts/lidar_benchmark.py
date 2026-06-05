"""LiDAR point clouds (LAS) — our columnar delta+context codec vs zstd / xz.

A LAS file stores N point records, each interleaving scaled-integer X/Y/Z, intensity,
GPS time, RGB and small categorical fields. Points have strong spatial locality
(consecutive deltas ≪ range), so the win is **de-interleave the fields into columns,
then first-difference the spatial/temporal ones** before entropy coding — exactly what
our numeric pipeline (transform + ctxcoder) does. We compare against general codecs on
the raw records; the domain specialist is **LAZ (LASzip)**, which typically reaches
~5–15× on airborne LiDAR (cited as reference — needs laszip, not run here).

LAS parsed by hand (numpy structured dtype) so no GIS libs are needed. Point formats
0–3 supported. Public sample: PDAL `test/data/las/autzen_trim.las`.

Usage: python3 scripts/lidar_benchmark.py <file.las>
"""
import os
import struct
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from compressor import ctxcoder

# field layout per LAS point-data-record format (name, numpy dtype, delta?)
_BASE = [("X", "<i4", 1), ("Y", "<i4", 1), ("Z", "<i4", 1), ("intensity", "<u2", 1),
         ("flags", "u1", 0), ("classification", "u1", 0), ("scan_angle", "i1", 0),
         ("user", "u1", 0), ("point_source", "<u2", 0)]
_GPS = [("gps_time", "<f8", 1)]
_RGB = [("red", "<u2", 1), ("green", "<u2", 1), ("blue", "<u2", 1)]
_FORMATS = {0: _BASE, 1: _BASE + _GPS, 2: _BASE + _RGB, 3: _BASE + _GPS + _RGB}


def ext(data, cmd):
    return len(subprocess.run(cmd, input=data, stdout=subprocess.PIPE, check=True).stdout)


def col_ours(col):
    """Smallest of ctxcoder on the int64 column or its first difference; round-trip checked."""
    x = col.astype(np.int64)
    best = None
    for arr, inv in ((x, lambda a: a),
                     (np.concatenate([x[:1], np.diff(x)]), np.cumsum)):
        cb = ctxcoder.encode(arr)
        assert np.array_equal(np.asarray(inv(np.asarray(ctxcoder.decode(cb, len(arr))))), x)
        best = len(cb) if best is None else min(best, len(cb))
    return best


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    raw = open(sys.argv[1], "rb").read()
    if raw[:4] != b"LASF":
        sys.exit("not a LAS file")
    off = struct.unpack_from("<I", raw, 96)[0]
    fmt = raw[104]
    reclen = struct.unpack_from("<H", raw, 105)[0]
    npts = struct.unpack_from("<I", raw, 107)[0]
    if fmt not in _FORMATS:
        sys.exit(f"point format {fmt} not supported")
    fields = _FORMATS[fmt]
    dt = np.dtype([(n, t) for n, t, _ in fields])
    body = raw[off:off + npts * reclen]
    recs = np.frombuffer(body[: npts * dt.itemsize] if dt.itemsize == reclen
                         else body, dtype=np.uint8)
    # build a structured view (handles the case reclen == sum of field sizes)
    assert dt.itemsize == reclen, f"reclen {reclen} != field sum {dt.itemsize}"
    table = np.frombuffer(body, dtype=dt, count=npts)
    raw_pts = npts * reclen
    print(f"{os.path.basename(sys.argv[1])}  format {fmt}  {npts:,} points  "
          f"{raw_pts/1e6:.2f} MB point data")

    zst = ext(body, ["zstd", "-19", "-c"])
    xzs = ext(body, ["xz", "-9", "-c"])
    ours = 0
    for name, t, _ in fields:
        col = table[name]
        if col.dtype.kind == "f":
            col = col.view(np.int64)               # GPS time: code the bit pattern's delta
        ours += col_ours(col)
    print(f"  {'zstd -19':<16}{zst/1e6:>7.2f} MB   {raw_pts/zst:5.2f}x")
    print(f"  {'xz -9':<16}{xzs/1e6:>7.2f} MB   {raw_pts/xzs:5.2f}x")
    print(f"  {'ours (col+delta)':<16}{ours/1e6:>7.2f} MB   {raw_pts/ours:5.2f}x   "
          f"[{'WIN' if ours < min(zst, xzs) else 'lose'} vs general; LAZ specialist ~5-15× (not run)]")


if __name__ == "__main__":
    main()
