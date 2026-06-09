"""End-to-end float64 compression in our codec (not just the transform proxy).

Chunk a real measured float64 column into many small files, train a model on 80%,
and compress the held-out 20% — the per-type amortized setup our codec targets.
Compare to zstd -19 / xz -9 on the raw test bytes. Round-trip verified."""
import os
import subprocess

import numpy as np

from pertype import codec
from pertype.model import train

CSV = os.environ.get("SCI_DATA", "data/sci") + "/household_power_consumption.txt"
CHUNK = 4096          # floats per "file" (32 KB)


def sh(cmd, data):
    return len(subprocess.run(cmd, input=data, stdout=subprocess.PIPE).stdout)


def columns(maxrows=600_000):
    cols = {2: [], 4: []}
    with open(CSV) as f:
        next(f)
        for line in f:
            p = line.rstrip("\n").split(";")
            if len(p) != 9 or "?" in p:
                continue
            try:
                cols[2].append(float(p[2])); cols[4].append(float(p[4]))
            except ValueError:
                continue
            if len(cols[2]) >= maxrows:
                break
    return {"Voltage (smooth)": np.asarray(cols[4]),
            "G_active (jumpy)": np.asarray(cols[2])}


def synth():
    rng = np.random.default_rng(0)
    n = 400_000
    t = np.arange(n)
    return {
        "synth random-walk": np.cumsum(rng.standard_normal(n)) * 0.01,
        # structured float64 where a value predictor (FCM/DFCM) has real signal:
        "synth sine": np.sin(t * 0.01) + 0.3 * np.sin(t * 0.071),
        "synth ramp+noise": t * 1.5 + rng.standard_normal(n) * 0.01,
    }


def chunks(arr):
    b = np.ascontiguousarray(arr, dtype=np.float64).tobytes()
    step = CHUNK * 8
    return [b[i:i + step] for i in range(0, len(b), step) if len(b[i:i + step]) == step]


def main():
    data = {}
    data.update(columns()); data.update(synth())
    for name, arr in data.items():
        files = chunks(arr)
        cut = len(files) * 4 // 5
        tr, te = files[:cut], files[cut:]
        model = train(tr, type_id="f64")
        raw = ours = z = x = 0
        for d in te:
            c = codec.compress(d, model)
            assert codec.decompress(c, model) == d, "ROUND-TRIP FAILED"
            raw += len(d); ours += len(c)
            z += sh(["zstd", "-19", "-c"], d); x += sh(["xz", "-9", "-c"], d)
        print(f"\n{name}: {len(te)} test files x {CHUNK} float64  (transform={model.transform})")
        print(f"  raw      {raw:>10,}  1.00x")
        print(f"  zstd-19  {z:>10,}  {raw/z:5.2f}x")
        print(f"  xz-9     {x:>10,}  {raw/x:5.2f}x")
        print(f"  ours     {ours:>10,}  {raw/ours:5.2f}x   "
              f"{'BEAT' if ours < min(z, x) else 'behind'} best general")


if __name__ == "__main__":
    main()
