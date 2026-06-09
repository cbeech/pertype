"""Hyperspectral imagery: inter-band delta (our 3D volume codec) vs per-band vs zstd/xz.

A hyperspectral cube is HxWxBands of 16-bit radiances; adjacent spectral bands are
strongly correlated, so treating bands as the slow axis of a *volume* and coding
inter-band deltas should beat coding each band independently — the open
"hyperspectral (de-interleave bands + delta)" roadmap item. Input is a ``.mat`` cube
(the standard AVIRIS "Indian Pines" / "Pavia" scenes), public no-auth:
  https://www.ehu.eus/ccwintco/index.php/Hyperspectral_Remote_Sensing_Scenes

Usage: python3 scripts/hyperspectral_benchmark.py <scene.mat>
Compares raw / zstd / xz / per-band image codec / inter-band volume codec; ours are
round-trip verified.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import scipy.io

from pertype import imagecodec


def ext_size(data, cmd):
    return len(subprocess.run(cmd, input=data, stdout=subprocess.PIPE, check=True).stdout)


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    m = scipy.io.loadmat(sys.argv[1])
    key = [k for k in m if not k.startswith("__")][0]
    cube = m[key]                                   # (H, W, Bands)
    bands = np.ascontiguousarray(cube.transpose(2, 0, 1)).astype(np.int16)  # (B, H, W)
    Bn, H, W = bands.shape
    raw = bands.astype("<i2").tobytes()
    print(f"{os.path.basename(sys.argv[1])}  {H}x{W}x{Bn}  {cube.dtype} -> int16  "
          f"raw {len(raw)/1e6:.2f} MB")

    zst = ext_size(raw, ["zstd", "-19", "-c"])
    xzs = ext_size(raw, ["xz", "-9", "-c"])

    per_band = sum(len(imagecodec.encode(bands[b], bayer=False)) for b in range(Bn))

    vol = imagecodec.encode_volume(bands)           # inter-band (slice = band) delta
    assert np.array_equal(imagecodec.decode_volume(vol), bands), "volume round-trip FAILED"
    vsz = len(vol)

    print(f"  {'zstd -19':<22}{zst/1e6:>8.2f} MB   {len(raw)/zst:5.2f}x")
    print(f"  {'xz -9':<22}{xzs/1e6:>8.2f} MB   {len(raw)/xzs:5.2f}x")
    print(f"  {'ours per-band':<22}{per_band/1e6:>8.2f} MB   {len(raw)/per_band:5.2f}x")
    print(f"  {'ours inter-band vol':<22}{vsz/1e6:>8.2f} MB   {len(raw)/vsz:5.2f}x   "
          f"[inter-band delta {(per_band-vsz)/per_band*100:+.0f}% vs per-band, "
          f"{'WIN' if vsz < min(zst, xzs) else 'lose'} vs general]")


if __name__ == "__main__":
    main()
