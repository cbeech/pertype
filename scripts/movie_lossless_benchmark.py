"""Is our *lossless* video codec up to par on real movie content?

Movies on disk are *lossy* distribution encodes (H.264/MPEG-2 at a few Mbit/s) —
already ~40-200x smaller than raw frames. A lossless codec preserves every pixel,
so it can never "beat" a lossy file; that's a category difference, not a fair race.
The honest, apples-to-apples question is: on real movie *frames*, is our lossless
codec competitive with the standard lossless codecs (FFV1, intra JPEG-XL)?

This decodes a short clip from a movie to raw 4:2:0 frames **locally** (bundled
ffmpeg from imageio-ffmpeg — nothing leaves the machine) and compares:
  raw YUV  vs  FFV1  vs  intra JPEG-XL  vs  ours (pertype.videocodec)
with a full round-trip check on ours. Sizes are bytes; ratios are vs raw YUV.

Usage:
  python3 scripts/movie_lossless_benchmark.py "<movie path>" [seek_seconds] [n_frames]
  python3 scripts/movie_lossless_benchmark.py --list   # sample some NAS movies

Everything is local. Personal media is never sent anywhere.
"""
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import imagecodecs as ic
import imageio_ffmpeg

from pertype import videocodec as vc
from pertype import cli

FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def decode_clip_to_y4m(movie, seek_s, n_frames, dst):
    """Decode n_frames starting at seek_s from `movie` into a 4:2:0 .y4m, locally.

    Maps the primary video stream (0:v:0 — not any attached cover-art image) and
    crops to a multiple of 32, so that the half-resolution 4:2:0 chroma planes are
    still a multiple of 16 (the codec's macroblock requirement)."""
    crop = "crop=trunc(iw/32)*32:trunc(ih/32)*32"
    cmd = [FFMPEG, "-y", "-hide_banner", "-loglevel", "error",
           "-ss", str(seek_s), "-i", movie, "-map", "0:v:0",
           "-frames:v", str(n_frames), "-vf", crop,
           "-pix_fmt", "yuv420p", "-an", "-sn",
           "-f", "yuv4mpegpipe", dst]
    subprocess.run(cmd, check=True)


def ffv1_size(y4m, workdir):
    out = os.path.join(workdir, "ffv1.nut")
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", y4m,
                    "-c:v", "ffv1", "-level", "3", "-coder", "1", "-context", "1",
                    "-f", "nut", out], check=True)
    return os.path.getsize(out)


def intra_jxl(stack):
    return sum(len(ic.jpegxl_encode(np.ascontiguousarray(stack[t]), lossless=True))
               for t in range(len(stack)))


def bench_clip(movie, seek_s, n_frames, work):
    name = os.path.basename(movie)
    y4m = os.path.join(work, "clip.y4m")
    decode_clip_to_y4m(movie, seek_s, n_frames, y4m)
    _, _, (Y, U, V) = cli._read_y4m(y4m)
    raw = Y.nbytes + U.nbytes + V.nbytes
    H, W = Y.shape[1], Y.shape[2]

    ffv1 = ffv1_size(y4m, work)
    jxl = intra_jxl(Y) + intra_jxl(U) + intra_jxl(V)
    t = time.time()
    blob = vc.encode_yuv(Y, U, V)
    enc_t = time.time() - t
    ours = len(blob)
    dec = vc.decode_yuv(blob)
    assert (np.array_equal(dec[0], Y) and np.array_equal(dec[1], U)
            and np.array_equal(dec[2], V)), "round-trip FAILED"

    st = vc.mode_stats(Y)          # Y-plane block mix explains the win/loss
    print(f"\n{name}  ({W}x{H}, {len(Y)} frames @ {seek_s}s)")
    print(f"  raw YUV    {raw/1e6:8.2f} MB   (1.00x)")
    print(f"  FFV1       {ffv1/1e6:8.2f} MB   ({raw/ffv1:5.2f}x)")
    print(f"  intra-JXL  {jxl/1e6:8.2f} MB   ({raw/jxl:5.2f}x)")
    print(f"  ours       {ours/1e6:8.2f} MB   ({raw/ours:5.2f}x)   "
          f"[{'WIN vs FFV1' if ours < ffv1 else 'lose vs FFV1'} "
          f"{(ffv1-ours)/ffv1*100:+.0f}%, enc {enc_t:.0f}s]")
    print(f"  Y blocks:  skip {st['skip_pct']:.0f}%  inter {st['inter_pct']:.0f}%  "
          f"intra {st['intra_pct']:.0f}%   "
          f"(inter+skip dominate -> our niche; intra-heavy -> FFV1's)")
    return raw, ffv1, jxl, ours


def sample_list():
    import glob
    print("Sample movies (probe codec with the bundled ffmpeg):")
    movies = os.environ.get("MOVIES_DIR", "data/movies")
    pats = [movies + "/*/*.mkv", movies + "/*/*.mp4"]
    files = []
    for p in pats:
        files += glob.glob(p)
    for f in sorted(files)[:12]:
        print(f"  {os.path.getsize(f)/1e9:6.2f} GB  {f}")


def main():
    if len(sys.argv) < 2 or sys.argv[1] == "--list":
        sample_list()
        return
    movie = sys.argv[1]
    seek_s = int(sys.argv[2]) if len(sys.argv) > 2 else 600
    n_frames = int(sys.argv[3]) if len(sys.argv) > 3 else 48
    work = tempfile.mkdtemp()
    bench_clip(movie, seek_s, n_frames, work)


if __name__ == "__main__":
    main()
