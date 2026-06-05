"""Climate / weather gridded data (HDF5 / NetCDF4): our float codec vs gzip / xz / zstd.

NCEP/NCAR reanalysis surface fields are float32 grids (time x lat x lon). They look noisy
byte-wise (the mantissa defeats prediction and XOR-delta — those lose to xz), but at fixed
precision the array holds **few distinct values**, which `compressor.floatcodec` exploits
losslessly: map each value's bit pattern to a dictionary index, then delta-code the smooth
index field. This beats xz on exactly this smooth-fixed-precision case (the standing
boundary). (Reading the file also exercises HDF5.)

Public no-auth source (NetCDF4/HDF5):
  https://downloads.psl.noaa.gov/Datasets/ncep.reanalysis/Dailies/surface/air.sig995.2012.nc

Usage: python3 scripts/weather_benchmark.py <file.nc> [var]   (needs h5py)
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from compressor import floatcodec


def sh(cmd, data):
    return len(subprocess.run(cmd, input=data, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, check=True).stdout)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    import h5py
    f = h5py.File(sys.argv[1], "r")
    var = sys.argv[2] if len(sys.argv) > 2 else next(
        k for k in f if isinstance(f[k], h5py.Dataset) and f[k].ndim >= 2)
    a = np.ascontiguousarray(f[var][...])
    raw = a.tobytes()
    N = len(raw)
    distinct = len(np.unique(a))
    print(f"{os.path.basename(sys.argv[1])}:{var}  {a.shape} {a.dtype}  {N/1e6:.2f} MB  "
          f"({distinct:,} distinct values = {100*distinct/a.size:.2f}%)")

    gz = sh(["gzip", "-9"], raw)
    xz = sh(["xz", "-9", "-c"], raw)
    zs = sh(["zstd", "-19", "-c"], raw)
    blob = floatcodec.encode(raw, a.dtype.itemsize)
    assert floatcodec.decode(blob) == raw, "round-trip FAILED"
    ours = len(blob)
    method = "store" if blob[4] == floatcodec.M_STORE else "dict+delta"

    for name, sz in [("gzip -9", gz), ("zstd -19", zs), ("xz -9", xz),
                     (f"ours ({method})", ours)]:
        print(f"  {name:<18}{sz/1e6:>8.2f} MB   {N/sz:6.2f}x")
    best = min(gz, xz, zs)
    print(f"  -> ours is {'WIN' if ours < best else 'lose'} vs general "
          f"({(best-ours)/best*100:+.0f}% vs best general)")


if __name__ == "__main__":
    main()
