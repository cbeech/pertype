"""Real FFV1 baseline: our inter-frame codec vs FFV1 (the standard intra-only
lossless video codec) and intra-only JPEG-XL, on full YUV .y4m clips.

FFV1 is provided by a static ffmpeg from the `imageio-ffmpeg` wheel (no system
install). FFV1 is intra-only (no motion compensation), so our motion-compensated
codec should win on static/slow content and stay competitive on motion. Ours is
`compressor.videocodec.encode_yuv` (round-trip verified here). Sizes are full YUV
vs the raw 4:2:0 bytes.

Usage: python3 scripts/video_ffv1_benchmark.py [n_frames]
"""
import glob
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import imagecodecs as ic
import imageio_ffmpeg

from compressor import videocodec as vc
from compressor import cli

NF = int(sys.argv[1]) if len(sys.argv) > 1 else 60
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


def truncate_y4m(src, n, dst):
    raw = open(src, "rb").read()
    nl = raw.index(b"\n")
    W = H = None
    for tok in raw[:nl].decode("ascii").split():
        if tok[0] == "W":
            W = int(tok[1:])
        elif tok[0] == "H":
            H = int(tok[1:])
    fs = W * H + 2 * ((W // 2) * (H // 2)) + len(b"FRAME\n")   # assumes plain FRAME
    with open(dst, "wb") as fh:
        fh.write(raw[:nl + 1] + raw[nl + 1:nl + 1 + n * fs])


def ffv1_size(y4m, workdir):
    out = os.path.join(workdir, "ffv1.nut")
    subprocess.run([FFMPEG, "-y", "-hide_banner", "-loglevel", "error", "-i", y4m,
                    "-c:v", "ffv1", "-level", "3", "-coder", "1", "-context", "1",
                    "-f", "nut", out], check=True)
    return os.path.getsize(out)


def intra_jxl(stack):
    return sum(len(ic.jpegxl_encode(np.ascontiguousarray(stack[t]), lossless=True))
               for t in range(len(stack)))


def main():
    work = tempfile.mkdtemp()
    print(f"frames={NF}   (sizes in MB; ratio vs raw YUV)")
    print(f"{'clip':<12}{'raw':>9}{'FFV1':>9}{'intra-JXL':>11}{'ours':>9}"
          f"{'ours/FFV1':>11}{'verdict':>8}")
    print("-" * 70)
    for path in sorted(glob.glob("/home/user/sci_data/video/*.y4m")):
        name = path.split("/")[-1].replace(".y4m", "")
        y4m = os.path.join(work, name + ".y4m")
        truncate_y4m(path, NF, y4m)
        _, _, (Y, U, V) = cli._read_y4m(y4m)
        raw = Y.nbytes + U.nbytes + V.nbytes

        ffv1 = ffv1_size(y4m, work)
        intra = intra_jxl(Y) + intra_jxl(U) + intra_jxl(V)
        t = time.time()
        blob = vc.encode_yuv(Y, U, V)
        enc_t = time.time() - t
        ours = len(blob)
        dec = vc.decode_yuv(blob)
        assert (np.array_equal(dec[0], Y) and np.array_equal(dec[1], U)
                and np.array_equal(dec[2], V)), f"round-trip FAILED {name}"

        gain = (ffv1 - ours) / ffv1 * 100
        verdict = "WIN" if ours < ffv1 else "lose"
        print(f"{name:<12}{raw/1e6:>8.2f}M{ffv1/1e6:>8.3f}M{intra/1e6:>10.3f}M"
              f"{ours/1e6:>8.3f}M{gain:>+10.0f}%{verdict:>8}"
              f"   (raw->ours {raw/ours:.2f}x, enc {enc_t:.0f}s)")


if __name__ == "__main__":
    main()
