"""Seismic / vibration — the high-rate, low-repetition prediction regime.

Real seismic waveforms (integer ADC counts from IRIS, parsed from the timeseries
ASCII output) are smooth, strongly autocorrelated, and have essentially no
exact-repeat structure — so an LZ coder (gzip/zstd/xz) has little to grab, while a
predictor + context-adaptive entropy coder should win, like audio and ECG. This
rounds out the prediction-friendly map.

Per segment: try the predictors {delta-1, fixed-order-2, +LMS cascade} and code
the residual with our native context coder (`ctxcoder`) or adaptive Rice; keep
the best, verified round-trip. Compared to gzip/zstd/xz on the raw int32 bytes.

Usage: python3 scripts/seismic_benchmark.py
"""
import os
import glob
import subprocess

import numpy as np

from compressor import ctxcoder, native

DIR = os.environ.get("SCI_DATA", "data/sci") + "/seismic"


def load(path):
    vals = []
    for line in open(path):
        parts = line.split()
        if not parts:
            continue
        try:
            vals.append(int(parts[-1]))
        except ValueError:
            continue                      # header / gap line
    return np.asarray(vals, dtype=np.int64)


def sh(cmd, data):
    return len(subprocess.run(cmd, input=data, stdout=subprocess.PIPE).stdout)


def _d1f(x):
    d = x.copy(); d[1:] = x[1:] - x[:-1]; return d
def _d1i(e):
    return np.cumsum(e).astype(np.int64)

PREDICTORS = {
    "delta1": (_d1f, _d1i),
    "fixed2": (native.fixed2_fwd, native.fixed2_inv),
    "fixed2+lms16": (lambda x: native.lms_fwd(native.fixed2_fwd(x), 16, 10),
                     lambda e: native.fixed2_inv(native.lms_inv(e, 16, 10))),
    "fixed2+lms16+256": (
        lambda x: native.lms_fwd(native.lms_fwd(native.fixed2_fwd(x), 16, 10), 256, 13),
        lambda e: native.fixed2_inv(native.lms_inv(native.lms_inv(e, 256, 13), 16, 10))),
}


def best_ours(x):
    best = None                           # (size, label)
    for name, (fwd, inv) in PREDICTORS.items():
        res = fwd(x)
        for coder, enc, dec in (("ctx", ctxcoder.encode, ctxcoder.decode),
                                ("rice", native.rice_encode, native.rice_decode)):
            blob = enc(res)
            if best is None or len(blob) < best[0]:
                back = inv(np.asarray(dec(blob, len(res)), dtype=np.int64))
                assert np.array_equal(back, x), f"round-trip FAILED ({name}/{coder})"
                best = (len(blob), f"{name}/{coder}")
    return best


def main():
    paths = sorted(glob.glob(f"{DIR}/*.txt"))
    print(f"{'segment':<20}{'samples':>9}{'gzip':>8}{'zstd':>8}{'xz':>8}{'ours':>8}"
          f"{'best predictor':>22}")
    print("-" * 83)
    for p in paths:
        x = load(p)
        if len(x) < 1000:
            print(f"{p.split('/')[-1]:<20}  (too short / empty)")
            continue
        raw = x.astype(np.int32).tobytes()
        n = len(raw)
        gz = sh(["gzip", "-9"], raw)
        zs = sh(["zstd", "-19", "-c"], raw)
        xz = sh(["xz", "-9", "-c"], raw)
        sz, label = best_ours(x)
        name = p.split("/")[-1].replace(".txt", "")
        verdict = "BEAT xz" if sz < xz else "below xz"
        print(f"{name:<20}{len(x):>9,}{n/gz:>7.2f}x{n/zs:>7.2f}x{n/xz:>7.2f}x{n/sz:>7.2f}x"
              f"{label:>16} {verdict}")


if __name__ == "__main__":
    main()
