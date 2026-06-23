"""Measure-first: microscopy / EM / micro-CT / 4D-STEM stacks (target #5).

Premise (backlog): smooth-in-space-and-time uint16 volumetric data where the incumbent
(Blosc + byte-shuffle + zstd) has no spatial/temporal predictor, so our volume codec
(`imagecodec.encode_volume`: inter-slice delta + 2D prediction) should win.

Reality on real data — this target does NOT pan out cleanly:
- **Confocal fluorescence (uint16)** — real `skimage` cells3d z-stack — is photon-noisy, not
  smooth: ratios are low (1.3-2.0×), inter-slice delta doesn't help (z-slices aren't redundant),
  and the result is data-dependent: ours beats the Blosc bar on the membrane channel (~+6%) but
  LOSES on the nuclei channel (~-10%, where plain zstd/xz win).
- **Cryo-ET tomograms (float32, EMPIAR-11058)** — genuinely smooth — still compress only ~1.2×
  for Blosc AND every predictor, because float32 low-mantissa bits are noise (≈9.6M distinct
  values): near-incompressible losslessly, predictor irrelevant.

So the codec's real volumetric win is on *clean integer* smooth volumes (medical CT/MR/DICOM —
already covered), not on these new noisy/float microscopy-EM cases. 4D-STEM diffraction
(uint8/16, smooth disks on dark) is the one untested sub-case that could differ.

This script reproduces the confocal uint16 measurement. Bar: Blosc shuffle+zstd (exact, via
`blosc2`) plus zstd/xz. Ours: per-slice and inter-slice volume codec, round-trip verified.
Data: real `skimage.data.cells3d` (auto-fetched), or set MICRO_NPY to a (nslices,H,W) uint16 .npy.
"""
import os
import subprocess
import time

import numpy as np

from pertype import imagecodec


def ext(data, cmd):
    return len(subprocess.run(cmd, input=data, stdout=subprocess.PIPE).stdout)


def load_stacks():
    npy = os.environ.get("MICRO_NPY")
    if npy:
        a = np.load(npy)
        return {os.path.basename(npy): np.ascontiguousarray(a)}
    from skimage.data import cells3d
    v = cells3d()  # (z=60, ch=2, y=256, x=256) uint16 confocal microscopy
    return {"cells3d-membrane": np.ascontiguousarray(v[:, 0]),
            "cells3d-nuclei": np.ascontiguousarray(v[:, 1])}


def bench(name, vol):
    assert vol.dtype == np.uint16 and vol.ndim == 3
    raw = vol.tobytes()
    n = len(raw)
    print(f"\n{name}: {vol.shape} uint16   raw {n / 1e6:.2f} MB")
    print(f"  {'method':<26}{'size (MB)':>12}{'ratio':>9}")

    def row(label, size):
        print(f"  {label:<26}{size / 1e6:>12.3f}{n / size:>9.2f}")

    row("zstd -19", ext(raw, ["zstd", "-19", "-c"]))
    row("xz -9", ext(raw, ["xz", "-9", "-c"]))
    try:
        import blosc2
        bar = len(blosc2.compress(raw, typesize=2, clevel=9,
                                  filter=blosc2.Filter.SHUFFLE, codec=blosc2.Codec.ZSTD))
        row("blosc shuffle+zstd (BAR)", bar)
    except Exception:
        bar = ext(raw, ["zstd", "-19", "-c"]); row("zstd (BAR; no blosc2)", bar)

    ps = sum(len(imagecodec.encode(vol[i], bayer=False)) for i in range(vol.shape[0]))
    row("ours per-slice", ps)
    vblob = imagecodec.encode_volume(vol)
    assert np.array_equal(imagecodec.decode_volume(vblob), vol), "volume round-trip FAILED"
    row("ours volume (inter-slice)", len(vblob))
    best = min(ps, len(vblob))
    print(f"  -> ours best vs blosc bar: {(bar - best) / bar * 100:+.1f}%  "
          f"({'win' if best < bar else 'LOSE'})   round-trip OK")


def main():
    t = time.time()
    for name, vol in load_stacks().items():
        bench(name, vol)
    print(f"\n[{time.time() - t:.1f}s]  Verdict: confocal microscopy is too noisy for the volume "
          f"predictor to win cleanly — target #5 down-ranked (see docs/data-type-opportunities.md).")


if __name__ == "__main__":
    main()
