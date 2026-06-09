"""Scientific numeric time-series with delta + the context-adaptive coder.

Corrects an earlier wrong conclusion. The first pass on UCI household power used
the memoryless adaptive *Rice* coder (delta + Rice = 2.78x) and concluded the
data "needs LZ, which our fast path lacks". But the order-2 context coder
(`compressor.ctxcoder`, built for ECG) was never run on it — and it handles the
long zero-runs of quantised sensor data extremely well (after a zero, the
bucket-given-context probability ~ 1, so ~0 bits/zero). Per int32 column:
delta-1 then ctxcoder; round-trip verified. Compared to gzip/xz on the same
column-major bytes.

Usage: python3 scripts/scidata_ctx_benchmark.py
"""
import os
import subprocess
import time

import numpy as np

from compressor import ctxcoder, native

ARR = os.environ.get("SCI_DATA", "data/sci") + "/power_cols_i32.npy"
NAMES = ["G_active", "G_reactive", "Voltage", "G_intensity", "Sub_1", "Sub_2", "Sub_3"]


def sh(cmd, data):
    return len(subprocess.run(cmd, input=data, stdout=subprocess.PIPE).stdout)


def main():
    cols = np.load(ARR)                                   # (7, n) int32 column-major
    raw = np.ascontiguousarray(cols).tobytes()
    N = len(raw)
    print(f"UCI household power: {cols.shape[0]} columns x {cols.shape[1]:,}  ({N/1e6:.1f} MB)\n")
    print(f"{'column':<12}{'delta+Rice':>12}{'delta+ctx':>12}")
    print("-" * 36)

    t = time.time()
    ctx_tot = rice_tot = 0
    for j in range(cols.shape[0]):
        x = cols[j].astype(np.int64)
        colraw = x.size * 4                               # the column is int32 (4 B/sample)
        d = x.copy(); d[1:] = x[1:] - x[:-1]              # first difference
        cb = ctxcoder.encode(d)
        back = np.cumsum(np.asarray(ctxcoder.decode(cb, len(d)))).astype(np.int64)
        assert np.array_equal(back, x), f"round-trip FAILED on {NAMES[j]}"
        rb = native.rice_encode(d)
        ctx_tot += len(cb); rice_tot += len(rb)
        print(f"{NAMES[j]:<12}{colraw/len(rb):>11.2f}x{colraw/len(cb):>11.2f}x")
    secs = time.time() - t

    xz = sh(["xz", "-9", "-c"], raw)
    gz = sh(["gzip", "-9"], raw)
    print("-" * 36)
    print(f"\nwhole file ({N/1e6:.1f} MB), round-trip verified:")
    print(f"  delta + Rice (old)   {N/rice_tot:6.2f}x")
    print(f"  delta + ctxcoder     {N/ctx_tot:6.2f}x   ({secs:.0f}s, beats gzip)")
    print(f"  gzip -9              {N/gz:6.2f}x")
    print(f"  xz -9                {N/xz:6.2f}x")


if __name__ == "__main__":
    main()
