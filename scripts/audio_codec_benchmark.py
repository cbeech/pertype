"""Benchmark the dedicated lossless audio codec (compressor.audiocodec) vs FLAC.

Real music decoded to 16-bit stereo PCM (libsndfile). Each track's chunk is
compressed by ours and by FLAC; both are decoded back and verified bit-exact
(no false positives). gzip/zstd on the raw PCM included for reference. Reports
per-track ratios, the mean, and how often ours beats FLAC.

Note: the adaptive filters converge over time, so longer segments favour ours;
these chunks are kept modest because the pure-Python filters are slow.

Usage: python3 scripts/audio_codec_benchmark.py [n_tracks] [chunk_samples]
"""
import glob
import io
import os
import statistics
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gzip
import numpy as np
import soundfile as sf

from compressor import audiocodec as ac

MUSIC = "/mnt/personal_folder/Music/iTunes"


def flac_size(pcm, sr):
    buf = io.BytesIO()
    sf.write(buf, pcm, sr, format="FLAC", subtype="PCM_16")
    buf.seek(0)
    assert np.array_equal(sf.read(buf, dtype="int16")[0], pcm), "FLAC not lossless!"
    return buf.getbuffer().nbytes


def main():
    n_tracks = int(sys.argv[1]) if len(sys.argv) > 1 else 10
    chunk = int(sys.argv[2]) if len(sys.argv) > 2 else 131072
    paths = sorted(glob.glob(os.path.join(MUSIC, "**", "*.mp3"), recursive=True))

    print(f"{'track':<34}{'ours':>7}{'FLAC':>7}{'gzip':>7}{'zstd':>7}", flush=True)
    print("-" * 62, flush=True)
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
        data = c.tobytes()
        n = len(data)

        blob = ac.encode(c, sr)
        assert np.array_equal(ac.decode(blob)[0], c), "ROUND-TRIP FAILED"
        flac = flac_size(c, sr)
        gz = len(gzip.compress(data, 9))
        zs = len(subprocess.run(["zstd", "-19", "-c"], input=data,
                                stdout=subprocess.PIPE).stdout)
        r = dict(ours=n / len(blob), flac=n / flac, gzip=n / gz, zstd=n / zs)
        rows.append(r)
        print(f"{os.path.basename(p)[:33]:<34}{r['ours']:>7.2f}{r['flac']:>7.2f}"
              f"{r['gzip']:>7.2f}{r['zstd']:>7.2f}", flush=True)

    print("-" * 62, flush=True)
    mean = {k: statistics.mean(row[k] for row in rows) for k in rows[0]}
    print(f"{'MEAN':<34}{mean['ours']:>7.2f}{mean['flac']:>7.2f}"
          f"{mean['gzip']:>7.2f}{mean['zstd']:>7.2f}", flush=True)
    wins = sum(1 for row in rows if row["ours"] > row["flac"])
    adv = statistics.mean((row["ours"] - row["flac"]) / row["flac"] for row in rows)
    print(f"\nours beats FLAC on {wins}/{len(rows)} tracks; mean advantage {adv*100:+.1f}%",
          flush=True)


if __name__ == "__main__":
    main()
