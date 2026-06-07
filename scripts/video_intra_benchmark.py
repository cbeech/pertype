"""Measure-first: would CALIC-class intra beat the video codec's MED+ctxcoder intra?

The video roadmap's open lever was "stronger intra" — high-motion frames are ~89%
intra-coded and our intra is plain MED, while FFV1 uses context-modelled intra. The
hypothesis: swapping the intra path for the CALIC predictor + energy-conditioned coding
(already in predictors.py) would convert the high-motion losses.

Frame 0 is all-intra and high-motion frames are ~89% intra, so coding a *whole* Y frame
with each method is a sound (slightly optimistic) proxy for the intra-path gain. We compare,
per real CIF frame, the coded bytes of:
  * current intra : ctxcoder.encode(frame - MED(frame))
  * CALIC         : predictors.calic_full_encode(frame)

Result (akiyo=low-motion, foreman=medium, stefan=high-motion — the target):
CALIC wins +4.6% / +4.2% on low/medium motion but only +0.36% on high-motion stefan.
The smooth content where CALIC shines is already inter/skip-coded in the real codec; on
high-motion (where intra dominates) the residual is near-random, so CALIC gains nothing.
Net realized gain is below the +3% bar and ~zero where it was supposed to help. NOT built.

Usage: PYTHONPATH=. python3 scripts/video_intra_benchmark.py [n_frames]
"""
import glob
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compressor import cli, ctxcoder, predictors

NF = int(sys.argv[1]) if len(sys.argv) > 1 else 30


def main():
    clips = sorted(glob.glob("/home/user/sci_data/video/*.y4m"))
    if not clips:
        print("no .y4m clips at ~/sci_data/video/ (local-only test pool)")
        return
    print(f"{'clip':<14}{'MED+ctx B':>12}{'CALIC B':>12}{'CALIC gain':>12}")
    print("-" * 50)
    tot_med = tot_cal = 0
    for path in clips:
        name = os.path.basename(path).replace(".y4m", "")
        _, _, planes = cli._read_y4m(path)
        Y = planes[0][:NF]  # (frames, H, W) uint8
        med = cal = 0
        for f in Y:
            res = f.astype(np.int64) - predictors.med_predict(f.astype(np.int32)).astype(np.int64)
            med += len(ctxcoder.encode(np.ascontiguousarray(res.reshape(-1), np.int64)))
            cal += len(predictors.calic_full_encode(np.ascontiguousarray(f.astype(np.int32)), 1))
        print(f"{name:<14}{med:>12d}{cal:>12d}{100 * (med - cal) / med:>11.2f}%")
        tot_med += med
        tot_cal += cal
    print("-" * 50)
    print(f"{'TOTAL':<14}{tot_med:>12d}{tot_cal:>12d}{100 * (tot_med - tot_cal) / tot_med:>11.2f}%")
    print("\n(+3% bar. The TOTAL over-counts — it treats every frame as all-intra; in the real "
          "codec the low/medium-motion frames are mostly inter-coded, so their CALIC gain is "
          "unrealized. The decisive cell is high-motion stefan: +0.36%.)")


if __name__ == "__main__":
    main()
