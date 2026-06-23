"""Measure-first: MRI raw k-space (real ISMRMRD, mridata.org Cartesian fully-sampled) — target #8.

Premise (the Tier-1 hypothesis): multi-coil k-space stores the SAME anatomy across 18–32 coils,
and low-frequency Fourier energy concentrates the signal — so there should be a lossless lever
(inter-coil redundancy and/or energy structure) that the field's storage form (raw complex
float32 in HDF5, optionally gzip/shuffle-filtered) leaves on the table.

VERDICT (real data: mridata.org Stanford fully-sampled 3D FSE knee, 18-coil, 384-sample Cartesian
readouts, complex float32; 500 imaging readouts ≈ 28 MB; round-trip verified):
- ❌ **RULED OUT.** The lossless ceiling is **~1.9×** and *everything* converges there:
  `xz -9` 1.89×, byte-shuffle+zstd (the HDF5 shuffle-filter specialist) 1.90×, pertype
  `floatcodec` 1.92×. floatcodec only **TIES** the shuffle bar (+0.9%, noise); `columnar`
  LOSES (−5.7% vs zlib — Δ inflates the spiky mantissa).
- The premise is **false for raw k-space.** Byte-plane entropy explains everything: plane0
  (low mantissa) = **0.00 bits (constant)** — the values are ~16-bit ADC precision padded into
  float32 — and plane3 (sign/exp) = 2.75 bits; planes 1–2 are near-random (5.8 / 8.0 bits).
  Sum ≈ 16.5 of 32 bits → a ~1.9× floor that is pure **byte-significance structure**, which the
  generic HDF5 shuffle filter ALREADY captures. There is no lever the specialist lacks.
- The "multi-coil redundancy" lever is **disconfirmed**: re/imag split (+2.0%), coil-major
  reorder (+1.9%), and reversible inter-coil delta (worse) all sit within noise of plain
  shuffle. Coils are decorrelated in the *raw* float values (different sensitivities/phases);
  the real anatomical redundancy lives in **image space (post-FFT)**, and exploiting it (coil
  combination / low-rank / parallel-imaging) is **lossy** — out of scope for a lossless codec.
- Same family as the ruled-out cryo-ET tomograms and LLM weights: **float-mantissa-limited,
  entropy floor already met by a generic byte-significance (shuffle) specialist.**

Data: a 396 MB ISMRMRD/HDF5 file from mridata.org (public). Readout size varies (calibration vs
imaging); we bucket by the dominant readout length and reshape each acquisition's interleaved
real/imag float32 `data` field into (coil, sample, [re,im]).
"""
import lzma
import os
import subprocess
import sys
import time
import urllib.request
import zlib

import h5py
import numpy as np

from pertype import floatcodec, columnar

URL = "http://mridata.org/download/25952770-5d0e-4cc1-917d-a77538f44a08"
PATH = sys.argv[1] if len(sys.argv) > 1 else "mri_kspace.h5"
NACQ = 500


def ensure_data():
    if not os.path.exists(PATH):
        print(f"downloading {URL} -> {PATH} (~396 MB, one-time) ...", flush=True)
        urllib.request.urlretrieve(URL, PATH)


def zstd(b, lvl=19):
    return subprocess.run(["zstd", f"-{lvl}", "-c"], input=b, stdout=subprocess.PIPE).stdout


def shuf(arr):
    """Byte-shuffle (split float32 into 4 byte-planes) then zstd — the HDF5 shuffle-filter bar."""
    u8 = arr.view(np.uint8).reshape(-1, 4)
    return zstd(np.ascontiguousarray(u8.T).tobytes())


def report(tag, arr, slow=False):
    arr = np.ascontiguousarray(arr, dtype="<f4")
    b = arr.tobytes()
    r = len(b)
    flt = floatcodec.encode(b, 4)
    assert floatcodec.decode(flt) == b                       # lossless guarantee
    res = {"zlib": len(zlib.compress(b, 9)), "zstd": len(zstd(b)),
           "shuf": len(shuf(arr)), "flt": len(flt)}
    if slow:
        res["xz"] = len(lzma.compress(b, preset=9))
        col = columnar.encode(b, width=4)
        assert columnar.decode(col) == b
        res["col"] = len(col)
    print(f"-- {tag}: raw {r / 1e6:.1f} MB")
    for k in ["zlib", "zstd", "xz", "shuf", "flt", "col"]:
        if k in res:
            print(f"     {k:5} {r / res[k]:5.2f}x  ({(1 - res[k] / res['zlib']) * 100:+5.1f}% vs zlib,"
                  f" {(1 - res[k] / res['shuf']) * 100:+5.1f}% vs shuf)", flush=True)
    return res


def main():
    ensure_data()
    f = h5py.File(PATH, "r")
    d = f["dataset"]["data"]
    ch = int(d[0]["head"]["active_channels"])
    # readout size varies (calibration vs imaging) -> bucket by data length, take the dominant
    lens = np.array([d[i]["data"].shape[0] for i in range(len(d))])
    vals, cnts = np.unique(lens, return_counts=True)
    dom = int(vals[cnts.argmax()])
    ns = dom // (ch * 2)
    idx = np.flatnonzero(lens == dom)[:NACQ]
    buf = np.stack([np.asarray(d[i]["data"], np.float32).reshape(ch, ns, 2) for i in idx])
    print(f"{len(idx)} readouts x {ch} coils x {ns} samples (complex64; dominant readout "
          f"{dom} of {len(d)} total)\n", flush=True)

    # byte-plane entropy — diagnose where the bits are (0=const, 8=random)
    flat = buf.reshape(-1).view(np.uint8).reshape(-1, 4)
    print("byte-plane entropy (bits/byte):")
    for p in range(4):
        c = np.bincount(flat[:, p], minlength=256) / len(flat)
        H = -(c[c > 0] * np.log2(c[c > 0])).sum()
        lbl = "(low mantissa)" if p == 0 else "(sign/exp)" if p == 3 else "(mantissa)"
        print(f"   plane{p} {lbl}: {H:.2f}", flush=True)
    print()

    # as-stored, then the three structure levers the multi-coil premise predicts
    report("as-stored (re/im interleaved)", buf, slow=True)
    report("re|im split", np.concatenate([buf[..., 0].ravel(), buf[..., 1].ravel()]))
    report("coil-major re|im", np.concatenate(
        [buf[:, c, :, 0].ravel() for c in range(ch)]
        + [buf[:, c, :, 1].ravel() for c in range(ch)]))
    re = buf[..., 0]
    icd = re.copy()
    icd[:, 1:, :] = re[:, 1:, :] - re[:, :-1, :]             # reversible inter-coil delta (real)
    report("inter-coil delta (real, float-space)",
           np.concatenate([icd.ravel(), buf[..., 1].ravel()]))


if __name__ == "__main__":
    t = time.time()
    main()
    print(f"\n[{time.time() - t:.0f}s]")
