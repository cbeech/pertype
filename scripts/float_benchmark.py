"""Lossless floating-point compression — a boundary test of the transform repertoire.

Our transforms (`delta`, `split`) are integer-oriented. IEEE-754 floats don't
subtract meaningfully in byte space, so the question is whether the repertoire
handles floats and what a float-specific primitive buys. We test:
  * the general tools (zstd / xz) on the raw float64 bytes;
  * `split(8)` — deinterleave the 8 byte-planes; the sign/exponent high bytes are
    nearly constant for similar-magnitude values;
  * `delta(8)` — stride-8 byte delta, expected to be ~useless on floats;
  * an **XOR-delta** primitive (Gorilla-style): XOR each float's bit pattern with
    the previous, so slowly-changing values leave mostly-zero bytes — then split.

Data: real measured float64 columns from UCI household power (Voltage — slowly
varying; Global_active_power — jumpy) plus a synthetic smooth float64 signal
(random walk, simulation-style). Transforms are round-trip verified.

Usage: python3 scripts/float_benchmark.py
"""
import os
import subprocess

import numpy as np

from compressor import transform

CSV = os.environ.get("SCI_DATA", "data/sci") + "/household_power_consumption.txt"
MAXROWS = 1_000_000
SPLIT8 = (("split", 8),)
DELTA8 = (("delta", 8),)


def sh(cmd, data):
    return len(subprocess.run(cmd, input=data, stdout=subprocess.PIPE).stdout)


def zstd(b):
    return sh(["zstd", "-19", "-c"], b)


def xz(b):
    return sh(["xz", "-9", "-c"], b)


def power_floats():
    cols = {2: [], 4: []}                        # Global_active_power, Voltage
    with open(CSV) as f:
        next(f)
        for line in f:
            p = line.rstrip("\n").split(";")
            if len(p) != 9 or "?" in p:
                continue
            try:
                v2, v4 = float(p[2]), float(p[4])
            except ValueError:
                continue
            cols[2].append(v2); cols[4].append(v4)
            if len(cols[2]) >= MAXROWS:
                break
    return {"power Voltage": np.asarray(cols[4]), "power G_active": np.asarray(cols[2])}


def synth_smooth(n=1_000_000):
    rng = np.random.default_rng(0)
    return {"synth random-walk": np.cumsum(rng.standard_normal(n)) * 0.01}


def xor_delta(arr):
    """XOR each float64 bit-pattern with the previous (Gorilla). Reversible:
    running XOR reconstructs. Returns the XOR'd bytes."""
    u = np.ascontiguousarray(arr, dtype=np.float64).view(np.uint64).copy()
    out = u.copy()
    out[1:] ^= u[:-1]
    inv = np.bitwise_xor.accumulate(out)         # running XOR == original bit patterns
    assert np.array_equal(inv.view(np.float64), arr), "XOR-delta not reversible"
    return out.tobytes()


def main():
    # sanity: split(8)/delta(8) are exactly reversible on float bytes
    probe = np.arange(64, dtype=np.float64).tobytes()
    for spec in (SPLIT8, DELTA8):
        assert transform.invert(transform.apply(probe, spec), spec) == probe

    data = {}
    data.update(power_floats())
    data.update(synth_smooth())

    for name, arr in data.items():
        arr = np.ascontiguousarray(arr, dtype=np.float64)
        raw = arr.tobytes()
        xd = xor_delta(arr)
        n = len(raw)
        rows = [
            ("raw + zstd", zstd(raw)),
            ("raw + xz", xz(raw)),
            ("delta8 + zstd", zstd(transform.apply(raw, DELTA8))),
            ("split8 + zstd", zstd(transform.apply(raw, SPLIT8))),
            ("split8 + xz", xz(transform.apply(raw, SPLIT8))),
            ("xor-delta + zstd", zstd(xd)),
            ("xor-delta + split8 + zstd", zstd(transform.apply(xd, SPLIT8))),
            ("xor-delta + split8 + xz", xz(transform.apply(xd, SPLIT8))),
        ]
        best = max(rows, key=lambda kv: n / kv[1])
        print(f"\n{name}: {len(arr):,} float64 ({n/1e6:.1f} MB)")
        for k, sz in rows:
            print(f"  {k:<28}{n/sz:>7.2f}x")
        print(f"  -> best: {best[0]} ({n/best[1]:.2f}x)")


if __name__ == "__main__":
    main()
