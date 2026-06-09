"""Measure-first: would CALIC-class intra beat the video codec's MED+ctxcoder intra?

The video roadmap's open lever was "stronger intra" — high-motion frames are ~89%
intra-coded and our intra is plain MED, while FFV1 uses context-modelled intra. The
hypothesis: swapping the intra path for the CALIC predictor + energy-conditioned coding
(already in predictors.py) would convert the high-motion losses.

Two measurements, the second decisive:

1. Whole-frame proxy — code each Y frame with both methods (current intra =
   ctxcoder.encode(frame - MED(frame)); CALIC = predictors.calic_full_encode(frame)).
   CALIC wins +4.6%/+4.2% on low/medium motion but only ~0.4% on high-motion stefan.

2. Mode-weighted realized gain — the real codec only intra-codes *some* blocks
   (videocodec.mode_stats gives the mix). The realized intra-path gain is bounded by
   intra_pct × (whole-frame CALIC gain), and it's an *upper* bound because the actually-intra
   blocks are the hard-to-predict regions (that's why they weren't inter-coded), where CALIC's
   smooth-content edge helps least. Measured:
     akiyo   intra  0.4%  × 4.56%  = 0.02%   (almost nothing is intra — it's 56% skip / 44% inter)
     foreman intra 27.4%  × 4.12%  = 1.13%
     stefan  intra 37.0%  × 0.40%  = 0.15%   (high-motion: CALIC barely beats MED here)
   All far below the +3% bar. The roadmap's "high-motion is ~89% intra" premise also fails on
   these clips (stefan is 37%). The lever is dead from both ends: where intra is common CALIC
   barely helps; where CALIC helps almost nothing is intra. NOT built.

Usage: PYTHONPATH=. python3 scripts/video_intra_benchmark.py [n_frames]
"""
import glob
import os
import sys

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compressor import cli, ctxcoder, predictors, videocodec

NF = int(sys.argv[1]) if len(sys.argv) > 1 else 16


def main():
    clips = sorted(glob.glob(os.environ.get("SCI_DATA", "data/sci") + "/video/*.y4m"))
    if not clips:
        print("no .y4m clips found; set SCI_DATA to a dir containing video/*.y4m (local-only)")
        return
    print(f"{'clip':<13}{'intra%':>8}{'inter%':>8}{'skip%':>8}"
          f"{'CALIC frame':>13}{'realized(UB)':>14}")
    print("-" * 64)
    for path in clips:
        name = os.path.basename(path).replace(".y4m", "")
        _, _, planes = cli._read_y4m(path)
        Y = planes[0][:NF]  # (frames, H, W) uint8
        st = videocodec.mode_stats(Y)                       # real per-block mode mix
        med = cal = 0
        for f in Y:
            res = f.astype(np.int64) - predictors.med_predict(f.astype(np.int32)).astype(np.int64)
            med += len(ctxcoder.encode(np.ascontiguousarray(res.reshape(-1), np.int64)))
            cal += len(predictors.calic_full_encode(np.ascontiguousarray(f.astype(np.int32)), 1))
        g = 100 * (med - cal) / med                         # whole-frame CALIC gain
        realized = st["intra_pct"] / 100.0 * g              # upper bound on the real gain
        print(f"{name:<13}{st['intra_pct']:>7.1f}%{st['inter_pct']:>7.1f}%{st['skip_pct']:>7.1f}%"
              f"{g:>12.2f}%{realized:>13.2f}%")
    print("-" * 64)
    print("\n+3% bar. realized(UB) = intra_pct x whole-frame CALIC gain — an UPPER bound (the "
          "actually-intra blocks are the hard regions where CALIC helps least, and only the "
          "residual sub-stream changes). All clips land far below the bar. Not built.")


if __name__ == "__main__":
    main()
