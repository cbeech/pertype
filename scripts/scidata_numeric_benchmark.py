"""Full-file benchmark on scientific numeric time-series (UCI household power,
exact int32 columnar milli-units).

The right per-type tool for integer time-series is NOT the text codec (dict + LZ
+ arithmetic — pure-Python, doesn't scale) but the native predictor + adaptive
Rice coder we built to beat FLAC. Here it's generalised to arbitrary integer
columns: per column we pick the predictor (order-1 delta / order-2 fixed / fixed
+ LMS cascade) that minimises the Rice-coded size — the per-type adaptivity that
is the whole thesis. Native, so the full 57 MB runs in seconds.

Compared against gzip/zstd/xz on the raw bytes, and against delta + zstd/xz
(which isolates how much is "delta" vs "our coder"). Every column is round-trip
verified through encode→decode.
"""
import os
import subprocess
import time

import numpy as np

from pertype import native, transform

ARR = os.environ.get("SCI_DATA", "data/sci") + "/power_cols_i32.npy"


def sh(cmd, data):
    t = time.time()
    out = subprocess.run(cmd, input=data, stdout=subprocess.PIPE).stdout
    return len(out), time.time() - t


# --- predictors (all int64, exactly reversible) -----------------------------

def _delta1_fwd(x):
    e = x.copy(); e[1:] = x[1:] - x[:-1]; return e

def _delta1_inv(e):
    return np.cumsum(e).astype(np.int64)

def _fixed2_fwd(x):
    return native.fixed2_fwd(x)

def _fixed2_inv(e):
    return native.fixed2_inv(e)

# predictor name -> (forward chain, inverse chain) on an int64 array
PREDICTORS = {
    "delta1":        (lambda x: _delta1_fwd(x),
                      lambda e: _delta1_inv(e)),
    "fixed2":        (lambda x: _fixed2_fwd(x),
                      lambda e: _fixed2_inv(e)),
    "fixed2+lms16":  (lambda x: native.lms_fwd(_fixed2_fwd(x), 16, 10),
                      lambda e: _fixed2_inv(native.lms_inv(e, 16, 10))),
    "fixed2+lms16+256": (
        lambda x: native.lms_fwd(native.lms_fwd(_fixed2_fwd(x), 16, 10), 256, 13),
        lambda e: _fixed2_inv(native.lms_inv(native.lms_inv(e, 256, 13), 16, 10))),
}


def best_column(x):
    """Pick the predictor that gives the smallest Rice-coded residual; verify."""
    best = None
    for name, (fwd, inv) in PREDICTORS.items():
        res = fwd(x)
        blob = native.rice_encode(res)
        if best is None or len(blob) < best[1]:
            best = (name, len(blob), blob, inv)
    name, size, blob, inv = best
    # round-trip verify this column
    res_back = native.rice_decode(blob, len(x))
    x_back = inv(res_back)
    assert np.array_equal(x_back, x), f"ROUND-TRIP FAILED on column ({name})"
    return name, size


def main():
    cols = np.load(ARR)                       # (7, n) int32 column-major
    blob = np.ascontiguousarray(cols).tobytes()
    n = len(blob)
    print(f"data: {n/1e6:.1f} MB  (int32 columnar, {cols.shape[0]} cols x {cols.shape[1]:,})\n")
    print(f"{'method':<26}{'size (MB)':>12}{'ratio':>9}{'time s':>9}")

    def row(name, size, secs):
        print(f"{name:<26}{size/1e6:>12.3f}{n/size:>9.2f}{secs:>9.1f}")

    row("gzip -9", *sh(["gzip", "-9"], blob))
    row("zstd -19", *sh(["zstd", "-19", "-c"], blob))
    row("xz -9", *sh(["xz", "-9", "-c"], blob))

    dblob = transform.apply(blob, (("delta", 4),))
    row("delta4 + zstd -19", *sh(["zstd", "-19", "-c"], dblob))
    row("delta4 + xz -9", *sh(["xz", "-9", "-c"], dblob))

    # ours: per-column predictor + native adaptive Rice
    t = time.time()
    total = 0
    picks = []
    for j in range(cols.shape[0]):
        x = cols[j].astype(np.int64)
        name, size = best_column(x)
        total += size
        picks.append(name)
    secs = time.time() - t
    # add tiny per-column header overhead (predictor id + length) to be honest
    total += cols.shape[0] * 6
    row("ours (predict+Rice)", total, secs)
    print(f"  per-column predictor: {picks}")
    print("  round-trip: OK (all columns verified)")


if __name__ == "__main__":
    main()
