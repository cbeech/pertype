"""Climate / weather gridded data (HDF5 / NetCDF4): an honest boundary on lossless float.

NCEP/NCAR reanalysis surface fields are float32 grids (time x lat x lon) that are smooth
in space and time, so general codecs already do well (xz ~3.2x). Our float decorrelators
(Gorilla XOR-delta, FCM) don't beat xz here, and the values don't map losslessly to scaled
integers (float32 rounding), so the big int-delta win doesn't apply. Like FITS float32, this
is near the lossless-float boundary — reported honestly rather than chased. (Reading the file
also exercises HDF5.)

Public no-auth source (NetCDF4/HDF5):
  https://downloads.psl.noaa.gov/Datasets/ncep.reanalysis/Dailies/surface/air.sig995.2012.nc

Usage: python3 scripts/weather_benchmark.py <file.nc> [var]   (needs h5py)
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from compressor import ctxcoder, transform


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
    print(f"{os.path.basename(sys.argv[1])}:{var}  {a.shape} {a.dtype}  {N/1e6:.2f} MB")

    gz = sh(["gzip", "-9"], raw)
    xz = sh(["xz", "-9", "-c"], raw)
    zs = sh(["zstd", "-19", "-c"], raw)
    # our best float decorrelator: Gorilla XOR-delta + byte-plane split, then ctxcoder
    if a.dtype == np.float32:
        xb = transform.apply(raw, (("xor", 4), ("split", 4)))
    else:
        xb = transform.apply(raw, (("xor", 8), ("split", 8)))
    ours = len(ctxcoder.encode(np.frombuffer(xb, np.uint8).astype(np.int64)))

    for name, sz in [("gzip -9", gz), ("zstd -19", zs), ("xz -9", xz),
                     ("ours (xor+ctx)", ours)]:
        print(f"  {name:<18}{sz/1e6:>8.2f} MB   {N/sz:6.2f}x")
    print(f"  -> {'WIN' if ours < min(gz, xz, zs) else 'BOUNDARY'}: smooth float32 is "
          f"compressible by everyone; our float tools don't beat xz (cf. FITS float32).")


if __name__ == "__main__":
    main()
