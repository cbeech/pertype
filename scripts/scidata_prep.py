"""Parse the UCI household power dataset into an exact lossless numeric array.

The 7 measurement columns are fixed-point to 3 decimals, so scaling by 1000 gives
an exact integer representation (milli-units) — the natural lossless form a
scientific/columnar store would hold, and the clean test for the delta transform.
Rows with missing values ('?') are dropped. Result cached column-major as .npy.
"""
import sys
import numpy as np

SRC = "/home/user/sci_data/household_power_consumption.txt"
OUT = "/home/user/sci_data/power_cols_i32.npy"

rows = []
with open(SRC, "r") as f:
    next(f)  # header
    for line in f:
        parts = line.rstrip("\n").split(";")
        if len(parts) != 9 or "?" in parts:
            continue
        # columns 2..8 are the 7 numeric measurements (3-decimal fixed point)
        vals = parts[2:9]
        try:
            scaled = [int(round(float(v) * 1000)) for v in vals]
        except ValueError:
            continue
        rows.append(scaled)

arr = np.asarray(rows, dtype=np.int32)        # shape (n, 7), row-major
print(f"complete rows: {arr.shape[0]:,}  cols: {arr.shape[1]}")
print(f"per-column min/max:")
for j in range(arr.shape[1]):
    print(f"  col{j}: {arr[:, j].min():>10d} .. {arr[:, j].max():>10d}")
colmajor = np.ascontiguousarray(arr.T)         # (7, n) column-major
np.save(OUT, colmajor)
print(f"saved {OUT}  ({colmajor.nbytes/1e6:.1f} MB raw int32)")
