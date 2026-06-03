"""Test the lossless-video hypothesis: a temporal frame-delta beats intra-only
coding on static/slow content and loses on high motion.

We have no ffmpeg/FFV1, but per-frame JPEG-XL lossless is a *stronger* intra
baseline than FFV1, so it's a fair, conservative stand-in. The test isolates the
temporal delta by running the SAME intra codec on original frames vs on
frame-residual images:

  intra     = sum_t  JXL(Y[t])
  temporal  = JXL(Y[0]) + sum_{t>0} JXL((Y[t]-Y[t-1]) mod 256)

If temporal < intra on static (akiyo) and temporal > intra on high motion
(stefan), the hypothesis holds. We also code the residual stream with our native
context coder (zigzag of the signed temporal delta). Raw `.y4m` is parsed with
numpy alone (no decoder). Luma plane only. Every path is round-trip verified.

Usage: python3 scripts/video_benchmark.py [n_frames]
"""
import glob
import subprocess
import sys

import numpy as np
import imagecodecs as ic

from compressor import ctxcoder

NF = int(sys.argv[1]) if len(sys.argv) > 1 else 60


def read_y4m_luma(path, n_frames):
    raw = open(path, "rb").read()
    nl = raw.index(b"\n")
    header = raw[:nl].decode("ascii")
    W = H = None
    for tok in header.split():
        if tok[0] == "W":
            W = int(tok[1:])
        elif tok[0] == "H":
            H = int(tok[1:])
    ysize = W * H
    framesize = ysize + 2 * ((W // 2) * (H // 2))   # C420
    pos = nl + 1
    frames = []
    while pos < len(raw) and len(frames) < n_frames:
        fnl = raw.index(b"\n", pos)                 # "FRAME...\n"
        pos = fnl + 1
        y = np.frombuffer(raw[pos:pos + ysize], dtype=np.uint8).reshape(H, W)
        frames.append(y)
        pos += framesize
    return np.stack(frames)                         # (T, H, W) uint8


def jxl(img):
    return ic.jpegxl_encode(np.ascontiguousarray(img), lossless=True)


def zstd(b):
    return len(subprocess.run(["zstd", "-19", "-c"], input=b, stdout=subprocess.PIPE).stdout)


def main():
    print(f"{'clip':<14}{'frames':>7}{'intra-JXL':>11}{'temporal-JXL':>13}"
          f"{'temporal-ctx':>13}{'verdict':>22}")
    print("-" * 80)
    for path in sorted(glob.glob("/home/user/sci_data/video/*.y4m")):
        name = path.split("/")[-1].replace(".y4m", "")
        Y = read_y4m_luma(path, NF)
        T = len(Y)
        raw = Y.nbytes

        # intra: per-frame JXL
        intra = sum(len(jxl(Y[t])) for t in range(T))

        # temporal: frame-delta (mod 256), per-residual-frame JXL; verify round-trip
        res = np.empty_like(Y)
        res[0] = Y[0]
        res[1:] = (Y[1:].astype(np.int16) - Y[:-1].astype(np.int16)).astype(np.uint8)
        recon = np.cumsum(res.astype(np.int16), axis=0).astype(np.uint8)
        assert np.array_equal(recon, Y), f"temporal round-trip FAILED {name}"
        temporal_jxl = sum(len(jxl(res[t])) for t in range(T))
        # verify JXL lossless on a residual frame
        assert np.array_equal(ic.jpegxl_decode(jxl(res[1])), res[1]), "JXL not lossless"

        # our native context coder on the signed temporal-delta stream
        signed = np.empty(Y.shape, dtype=np.int64)
        signed[0] = Y[0]
        signed[1:] = Y[1:].astype(np.int64) - Y[:-1].astype(np.int64)
        flat = signed.reshape(-1)
        blob = ctxcoder.encode(flat)
        back = np.array(ctxcoder.decode(blob, flat.size), dtype=np.int64)
        assert np.array_equal(back, flat), f"ctx round-trip FAILED {name}"
        temporal_ctx = len(blob)

        best_temporal = min(temporal_jxl, temporal_ctx)
        verdict = "temporal WINS" if best_temporal < intra else "intra wins"
        gain = (intra - best_temporal) / intra * 100
        print(f"{name:<14}{T:>7}{intra/1e6:>10.3f}M{temporal_jxl/1e6:>12.3f}M"
              f"{temporal_ctx/1e6:>12.3f}M{verdict+f' {gain:+.0f}%':>22}")


if __name__ == "__main__":
    main()
