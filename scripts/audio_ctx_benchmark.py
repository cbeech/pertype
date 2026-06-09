"""Does the context-adaptive coder widen our FLAC win on real music?

Compares the audio codec with its two residual back-ends (Rice vs context-
adaptive arithmetic) against FLAC (libsndfile), on real local music decoded to
16-bit PCM. Both of ours are decoded back and verified bit-exact. Nothing leaves
the machine.

Usage: python3 scripts/audio_ctx_benchmark.py [n_tracks] [chunk_samples]
"""
import glob
import io
import os
import statistics
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import soundfile as sf

from compressor import audiocodec as ac

MUSIC = os.environ.get("MUSIC_DIR", "data/music")


def flac_size(pcm, sr):
    buf = io.BytesIO()
    sf.write(buf, pcm, sr, format="FLAC", subtype="PCM_16")
    buf.seek(0)
    assert np.array_equal(sf.read(buf, dtype="int16")[0], pcm), "FLAC not lossless!"
    return buf.getbuffer().nbytes


def main():
    n_tracks = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    chunk = int(sys.argv[2]) if len(sys.argv) > 2 else 131072
    paths = sorted(glob.glob(os.path.join(MUSIC, "**", "*.mp3"), recursive=True))

    print(f"{'track':<32}{'rice':>7}{'ctx':>7}{'FLAC':>7}", flush=True)
    print("-" * 53, flush=True)
    rows = []
    for p in paths:
        if len(rows) >= n_tracks:
            break
        try:
            pcm, sr = sf.read(p, dtype="int16")
        except Exception:
            continue
        if pcm.ndim != 2 or pcm.shape[0] < chunk * 2:
            continue
        c = np.ascontiguousarray(pcm[pcm.shape[0] // 3:][:chunk])
        n = c.nbytes

        er = ac.encode(c, sr, coder="rice")
        ex = ac.encode(c, sr, coder="ctx")
        assert np.array_equal(ac.decode(er)[0], c), "rice ROUND-TRIP FAILED"
        assert np.array_equal(ac.decode(ex)[0], c), "ctx ROUND-TRIP FAILED"
        fl = flac_size(c, sr)
        r = dict(rice=n / len(er), ctx=n / len(ex), flac=n / fl)
        rows.append(r)
        print(f"{os.path.basename(p)[:31]:<32}{r['rice']:>7.2f}{r['ctx']:>7.2f}{r['flac']:>7.2f}",
              flush=True)

    print("-" * 53, flush=True)
    mean = {k: statistics.mean(row[k] for row in rows) for k in rows[0]}
    print(f"{'MEAN':<32}{mean['rice']:>7.2f}{mean['ctx']:>7.2f}{mean['flac']:>7.2f}", flush=True)
    cw = sum(1 for r in rows if r["ctx"] > r["flac"])
    rw = sum(1 for r in rows if r["rice"] > r["flac"])
    cadv = statistics.mean((r["ctx"] - r["flac"]) / r["flac"] for r in rows)
    radv = statistics.mean((r["rice"] - r["flac"]) / r["flac"] for r in rows)
    print(f"\nvs FLAC:  rice beats {rw}/{len(rows)} (mean {radv*100:+.1f}%)   "
          f"ctx beats {cw}/{len(rows)} (mean {cadv*100:+.1f}%)", flush=True)


if __name__ == "__main__":
    main()
