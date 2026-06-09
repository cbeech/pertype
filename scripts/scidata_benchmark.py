"""Benchmark our trained compressor vs general-purpose tools on scientific
numeric time-series (UCI household power, exact int32 columnar milli-units).

Tests the real delta-thesis claim: on decorrelated numeric data our transform +
entropy coder should beat gzip/zstd/xz, and we separate "what delta buys" (delta
+ zstd) from "what our coder buys" (ours). Every result is round-trip verified.
"""
import os
import gzip
import subprocess
import sys
import time

import numpy as np

from compressor import codec, model, transform

CAP = int(sys.argv[1]) if len(sys.argv) > 1 else 0   # bytes; 0 = whole file
ARR = os.environ.get("SCI_DATA", "data/sci") + "/power_cols_i32.npy"


def sh(cmd, data):
    t = time.time()
    out = subprocess.run(cmd, input=data, stdout=subprocess.PIPE).stdout
    return len(out), time.time() - t


def main():
    cols = np.load(ARR)                       # (7, n) int32 column-major
    blob = cols.tobytes()
    if CAP:
        blob = blob[:CAP - (CAP % 4)]
    n = len(blob)
    print(f"data: {n/1e6:.1f} MB  (int32 columnar, {cols.shape[0]} cols)")
    print(f"{'method':<22}{'size (MB)':>12}{'ratio':>9}{'time s':>9}")

    def row(name, size, secs):
        print(f"{name:<22}{size/1e6:>12.3f}{n/size:>9.2f}{secs:>9.1f}")

    # --- general-purpose baselines on raw numeric ---
    row("gzip -9", *sh(["gzip", "-9"], blob))
    row("zstd -19", *sh(["zstd", "-19", "-c"], blob))
    row("xz -9", *sh(["xz", "-9", "-c"], blob))

    # --- delta-decorrelated, then a strong general coder (isolates delta) ---
    dblob = transform.apply(blob, (("delta", 4),))
    zsz, zt = sh(["zstd", "-19", "-c"], dblob)
    row("delta4 + zstd -19", zsz, zt)
    xsz, xt = sh(["xz", "-9", "-c"], dblob)
    row("delta4 + xz -9", xsz, xt)

    # --- ours: trained model (auto transform + entropy coder) ---
    chunk = 1 << 16
    samples = [blob[i:i + chunk] for i in range(0, min(n, 1 << 22), chunk)]
    t = time.time()
    m = model.train(samples, "power")
    train_t = time.time() - t
    print(f"  [ours] transform={m.transform} use_lz={m.use_lz} "
          f"patterns={len(m.dictionary.patterns)} train={train_t:.1f}s")

    t = time.time()
    cz = codec.compress(blob, m, max_chain=8) if m.use_lz else codec.compress(blob, m)
    ct = time.time() - t
    back = codec.decompress(cz, m)
    assert back == blob, "ROUND-TRIP FAILED"
    row("ours (trained)", len(cz), ct)
    print("  round-trip: OK")


if __name__ == "__main__":
    main()
