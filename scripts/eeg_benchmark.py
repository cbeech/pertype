"""Measure-first: EEG / iEEG / MEG multichannel biosignal (target #2).

Scalp/intracranial EEG is band-limited, autocorrelated multichannel int16 — the same signal
family as ECG, which our predictor + adaptive coder already beats FLAC/xz on. Much EEG is stored
as EDF (raw int16) then gzip'd; the specialist bar is MEF3 "RED" (a diff + range coder). A
per-channel predictor (fixed2 / sign-sign LMS cascade) + Rice/context coder should beat both.

Cross-channel prediction is NOT attempted — disconfirmed for electrophysiology (temporal
prediction already captures the redundancy; see the ephys row). Per-channel is the win.

Bar: gzip / zstd / xz on the raw int16 (≈ EDF+gzip storage), and delta+zstd (≈ MEF3 RED).
Ours: per-channel best-of predict+Rice (round-trip verified), and the full audio LMS codec.

Data: a CHB-MIT scalp-EEG EDF (PhysioNet, no auth). Default EEG_EDF, else download:
  curl -O https://physionet.org/files/chbmit/1.0.0/chb01/chb01_01.edf   # EEG_EDF=chb01_01.edf
"""
import os
import subprocess
import time

import numpy as np

from pertype import audiocodec, native, transform

EDF = os.environ.get("EEG_EDF", "data/eeg/chb01_01.edf")


def sh(cmd, data):
    return len(subprocess.run(cmd, input=data, stdout=subprocess.PIPE).stdout)


def load_edf(path):
    b = open(path, "rb").read()
    ns = int(b[252:256]); nrec = int(b[236:244]); hb = int(b[184:192])
    nsamp_off = 256 + ns * (16 + 80 + 8 * 5 + 80)
    nsamp = [int(b[nsamp_off + i * 8: nsamp_off + (i + 1) * 8]) for i in range(ns)]
    per_rec = sum(nsamp)
    d = np.frombuffer(b[hb:], dtype="<i2")[: nrec * per_rec]
    # each record holds nsamp[c] samples per channel, concatenated channel-major
    chans = [[] for _ in range(ns)]
    rec = d.reshape(nrec, per_rec)
    offs = np.cumsum([0] + nsamp)
    for c in range(ns):
        chans[c] = np.ascontiguousarray(rec[:, offs[c]:offs[c + 1]].reshape(-1))
    return chans


PREDICTORS = {
    "delta1": (lambda x: np.concatenate([[x[0]], np.diff(x)]),
               lambda e: np.cumsum(e).astype(np.int64)),
    "fixed2": (native.fixed2_fwd, native.fixed2_inv),
    "f2+lms16": (lambda x: native.lms_fwd(native.fixed2_fwd(x), 16, 10),
                 lambda e: native.fixed2_inv(native.lms_inv(e, 16, 10))),
    "f2+lms16+256": (
        lambda x: native.lms_fwd(native.lms_fwd(native.fixed2_fwd(x), 16, 10), 256, 13),
        lambda e: native.fixed2_inv(native.lms_inv(native.lms_inv(e, 256, 13), 16, 10))),
}


def predict_rice(x):
    best = None
    for name, (fwd, inv) in PREDICTORS.items():
        blob = native.rice_encode(fwd(x))
        if best is None or len(blob) < best[1]:
            best = (name, len(blob), inv, blob)
    name, size, inv, blob = best
    assert np.array_equal(inv(native.rice_decode(blob, len(x))), x), f"RT fail {name}"
    return size


def main():
    chans = load_edf(EDF)
    raw = b"".join(c.tobytes() for c in chans)
    n = len(raw)
    print(f"EEG: {len(chans)} channels x {len(chans[0]):,} samples   raw {n/1e6:.1f} MB int16\n")
    print(f"{'method':<28}{'ratio':>8}")

    def row(label, size):
        print(f"{label:<28}{n/size:>7.2f}x")

    row("gzip -9", sh(["gzip", "-9"], raw))
    bar = sh(["zstd", "-19", "-c"], raw); row("zstd -19 (EDF+gz form)", bar)
    row("xz -9", sh(["xz", "-9", "-c"], raw))
    row("delta2+zstd (~MEF3 RED)", sh(["zstd", "-19", "-c"], transform.apply(raw, (("delta", 2),))))

    t = time.time(); pr = sum(predict_rice(c.astype(np.int64)) + 6 for c in chans)
    row("ours predict+Rice", pr); tpr = time.time() - t

    t = time.time(); ac = 0
    for c in chans:
        enc = audiocodec.encode(c.astype(np.int16), 256)
        dec, _ = audiocodec.decode(enc)
        assert np.array_equal(dec.ravel(), c), "audio codec RT fail"
        ac += len(enc)
    row("ours audio codec (LMS)", ac)

    best = min(pr, ac)
    print(f"\nours vs zstd-19 (EDF+gz): {(bar-best)/bar*100:+.1f}%  "
          f"({'WIN' if best < bar else 'lose'})   round-trip OK   [{tpr+time.time()-t:.0f}s]")


if __name__ == "__main__":
    main()
