"""Lossless audio benchmark: ours vs FLAC / gzip / zstd on real 16-bit PCM.

Tracks are decoded to 16-bit stereo PCM (libsndfile). Each method compresses the
same PCM bytes; FLAC (libsndfile, the purpose-built lossless audio codec) is the
baseline, the audio analogue of PNG/JPEG-XL for images. Ours uses the per-type
transform + entropy (LZ disabled — it adds ~0 on decorrelated samples). Every
block is round-trip verified.

Usage: python3 scripts/audio_benchmark.py [n_tracks]
"""
import glob
import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import soundfile as sf

import pertype.model as M
from pertype.benchmark import _gzip_size, _zstd_size
from pertype.codec import compress, decompress

MUSIC = os.environ.get("MUSIC_DIR", "data/music")
M.BLOB_SPECS = (("none", 0),)  # transform + entropy; LZ adds ~0 on audio residuals


def flac_size(pcm, sr):
    buf = io.BytesIO()
    sf.write(buf, pcm, sr, format="FLAC", subtype="PCM_16")
    buf.seek(0)
    back, _ = sf.read(buf, dtype="int16")
    assert np.array_equal(back, pcm), "FLAC not lossless!"
    return buf.getbuffer().nbytes


def main():
    n_tracks = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    paths = sorted(glob.glob(os.path.join(MUSIC, "**", "*.mp3"), recursive=True))
    samples, srs = [], []
    for p in paths:
        if len(samples) >= n_tracks:
            break
        try:
            pcm, sr = sf.read(p, dtype="int16")
        except Exception:
            continue
        if pcm.ndim != 2 or pcm.shape[0] < 600000:
            continue
        # a ~2 MB chunk from the middle (skip intros/silence)
        start = pcm.shape[0] // 3
        samples.append(np.ascontiguousarray(pcm[start:start + 524288]))
        srs.append(sr)
    print(f"{len(samples)} tracks, 16-bit stereo, ~12s/2MB chunks", flush=True)

    cut = max(1, len(samples) * 3 // 4)
    train_s, test_s = samples[:cut], samples[cut:]
    model = M.train([c.tobytes() for c in train_s], type_id="audio")
    print(f"transform selected: {model.transform}; model {len(model.save()):,}B", flush=True)

    tot = dict(raw=0, ours=0, gzip=0, zstd=0, flac=0)
    for c, sr in zip(test_s, srs[cut:]):
        data = c.tobytes()
        comp = compress(data, model)
        assert decompress(comp, model) == data, "ROUND-TRIP FAILED"
        tot["raw"] += len(data)
        tot["ours"] += len(comp)
        tot["gzip"] += _gzip_size(data)
        tot["zstd"] += _zstd_size(data)
        tot["flac"] += flac_size(c, sr)
        print(f"  track: ours {len(data)/len(comp):.2f}x  flac {len(data)/tot['flac'] if False else len(data)/flac_size(c,sr):.2f}x", flush=True)

    raw = tot["raw"]
    print(f"\n{len(test_s)} held-out tracks:")
    for k, label in [("gzip", "gzip -9"), ("zstd", "zstd -19"),
                     ("ours", "ours (transform)"), ("flac", "FLAC")]:
        print(f"  {label:<18}{raw / tot[k]:.2f}x")


if __name__ == "__main__":
    main()
