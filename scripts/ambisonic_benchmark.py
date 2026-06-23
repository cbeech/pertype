"""Measure-first: multichannel / ambisonic (FOA) audio.

Premise (Tier-1 backlog #3): "extend the audio win along the axis FLAC barely models —
inter-channel redundancy." Ambisonic W/X/Y/Z capture one sound field, so they look
highly redundant. Question: after the existing per-channel temporal predictor runs, is
there an inter-channel lever left — and does per-channel pertype beat the FLAC bar?

VERDICT (3 real FOA recordings — Zenodo 13341921, 16-bit FOA field recordings):
- **Inter-channel lever RULED OUT.** A reversible integer cross-channel sign-sign LMS on
  the temporal residuals gives +0.1% (mensa/diffuse) / -3.2% (fridge/tonal) / +1.0%
  (traffic/directional) real coded bytes — flat to negative, *hurts* on tonal content.
  An oracle block-adaptive cross-predictor cuts residual *variance* by up to +37% on
  traffic, but that lives in a low-energy correlated subspace that does NOT move the Rice
  bit-cost (the variance-down-but-entropy-flat trap). Root cause = the ephys ruling: the
  temporal predictor already removes the cross-channel-exploitable structure, and diffuse
  sound fields decorrelate W/X/Y/Z by construction (raw |corr| ~0.09 on mensa).
- **Per-channel pertype only TIES FLAC.** ctx backend: +0.3% (mensa, both at the noise
  floor) / +2.1% (fridge) vs a libsndfile FLAC bar — and would look weaker vs `flac -8`.
  Diffuse ambience sits at the entropy floor where no predictor helps.
- Net: not a clean win. Down-ranked. Tested 16-bit FOA; HOA >=16ch and 24-bit close-mic
  multitrack untested, but the mechanism that kills it (temporal-prediction-first) is the
  same one that ruled out cross-channel ephys.

Data: Zenodo record 13341921 (First Order Ambisonics field recordings). Pass WAV paths;
the 16-bit PCM files (mensa.wav, the fridge loop) are the clean cases.
"""
import io
import sys

import numpy as np
import soundfile as sf

from pertype import audiocodec, native
from pertype.audiocodec import _predict_fwd

SECS = 20
SHIFT = 12  # cross-channel weight downshift


def _residuals(pcm):
    return np.stack([_predict_fwd(pcm[:, c].astype(np.int64)) for c in range(pcm.shape[1])], axis=1)


def _cross_lms(res):
    """Reversible integer inter-channel predictor: channel 0 unchanged; channel c
    predicted (instantaneously) from already-decoded channels 0..c-1 via sign-sign LMS.
    Decoder reconstructs each channel before it is needed -> exactly reversible."""
    n, C = res.shape
    out = res.copy()
    for c in range(1, C):
        w = np.zeros(c, dtype=np.int64)
        x = res[:, c].astype(np.int64)
        ctx = res[:, :c].astype(np.int64)
        e = np.empty(n, dtype=np.int64)
        for t in range(n):
            h = ctx[t]
            err = int(x[t]) - (int(w @ h) >> SHIFT)
            e[t] = err
            if err > 0:
                w += np.sign(h)
            elif err < 0:
                w -= np.sign(h)
        out[:, c] = e
    return out


def interchannel_test(pcm):
    """Real reversible cross-channel coding vs per-channel (Rice bytes)."""
    res = _residuals(pcm)
    base = sum(len(native.rice_encode(np.ascontiguousarray(res[:, c]))) for c in range(res.shape[1]))
    xres = _cross_lms(res)
    cross = sum(len(native.rice_encode(np.ascontiguousarray(xres[:, c]))) for c in range(res.shape[1]))
    return base, cross


def flac_bar(pcm, sr):
    buf = io.BytesIO()
    sf.write(buf, pcm, sr, format="FLAC", subtype="PCM_16")
    return len(buf.getbuffer())


def main(path):
    info = sf.info(path)
    n = min(info.frames, SECS * info.samplerate)
    pcm, sr = sf.read(path, frames=n, dtype="int16", always_2d=True)
    name = path.split("/")[-1][:26]
    C = pcm.shape[1]
    raw = n * C * 2

    raw_corr = np.corrcoef(pcm.astype(np.float64).T)
    off = np.abs(raw_corr[~np.eye(C, dtype=bool)])

    base, cross = interchannel_test(pcm)
    flac = flac_bar(pcm, sr)
    ctx = len(audiocodec.encode(pcm, sr, coder="ctx"))

    print(f"{name:28} {C}ch {n/sr:4.0f}s | raw|corr| {off.mean():.2f} "
          f"| inter-channel {(1-cross/base)*100:+.1f}% (reversible) "
          f"| pertype-ctx vs FLAC {(1-ctx/flac)*100:+.1f}% ({raw/ctx:.2f}x vs {raw/flac:.2f}x)")
    return base, cross


if __name__ == "__main__":
    tb = tc = 0
    for p in sys.argv[1:]:
        b, c = main(p)
        tb += b
        tc += c
    if tb:
        print(f"\nInter-channel lever across files: {(1-tc/tb)*100:+.1f}% "
              f"-> RULED OUT (temporal predictor already takes the structure).")
