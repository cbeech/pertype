"""Benchmark on a smooth, high-entropy scientific signal: PhysioNet Apnea-ECG
(single-lead ECG, 100 Hz, format-16 raw int16).

This is the data type our predictor + adaptive Rice coder is actually built for
(the same family that beats FLAC). Test vs gzip/zstd/xz on the raw int16 bytes,
vs delta + zstd/xz (isolating the transform), and run our full audio codec
(LMS cascade) directly since these are int16 waveforms. Every record round-trip
verified.
"""
import os
import glob
import subprocess
import time

import numpy as np

from compressor import audiocodec, native, transform

DIR = os.environ.get("SCI_DATA", "data/sci") + "/ecg"


def sh(cmd, data):
    t = time.time()
    out = subprocess.run(cmd, input=data, stdout=subprocess.PIPE).stdout
    return len(out), time.time() - t


def _d1f(x):
    e = x.copy(); e[1:] = x[1:] - x[:-1]; return e
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


def predict_rice(x):
    best = None
    for name, (fwd, inv) in PREDICTORS.items():
        blob = native.rice_encode(fwd(x))
        if best is None or len(blob) < best[1]:
            best = (name, len(blob), blob, inv)
    name, size, blob, inv = best
    assert np.array_equal(inv(native.rice_decode(blob, len(x))), x), f"RT fail {name}"
    return name, size


def main():
    files = sorted(glob.glob(f"{DIR}/*.dat"))
    arrays = [np.fromfile(f, dtype="<i2") for f in files]
    raw = b"".join(a.tobytes() for a in arrays)
    n = len(raw)
    total_samples = sum(len(a) for a in arrays)
    print(f"records: {len(files)}   samples: {total_samples:,}   raw: {n/1e6:.1f} MB int16\n")
    print(f"{'method':<26}{'size (MB)':>12}{'ratio':>9}{'time s':>9}")

    def row(name, size, secs):
        print(f"{name:<26}{size/1e6:>12.3f}{n/size:>9.2f}{secs:>9.1f}")

    row("gzip -9", *sh(["gzip", "-9"], raw))
    row("zstd -19", *sh(["zstd", "-19", "-c"], raw))
    row("xz -9", *sh(["xz", "-9", "-c"], raw))
    dblob = transform.apply(raw, (("delta", 2),))
    row("delta2 + zstd -19", *sh(["zstd", "-19", "-c"], dblob))
    row("delta2 + xz -9", *sh(["xz", "-9", "-c"], dblob))

    # ours: generic predictor + Rice (per record, best predictor)
    t = time.time(); total = 0; picks = []
    for a in arrays:
        name, size = predict_rice(a.astype(np.int64))
        total += size + 6; picks.append(name)
    row("ours (predict+Rice)", total, time.time() - t)
    print(f"  predictors: {picks}")

    # ours: full audio codec (LMS cascade) run directly on the int16 waveform
    t = time.time(); total = 0
    for a in arrays:
        enc = audiocodec.encode(a.astype(np.int16), 100)
        dec, _ = audiocodec.decode(enc)
        assert np.array_equal(dec.ravel(), a), "audio codec RT fail"
        total += len(enc)
    row("ours (audio codec)", total, time.time() - t)
    print("  round-trip: OK (all records verified)")


if __name__ == "__main__":
    main()
