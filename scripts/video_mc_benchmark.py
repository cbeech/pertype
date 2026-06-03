"""Can block motion compensation beat intra-only where a plain frame-delta loses?

The temporal frame-delta forces a zero motion vector, so it loses on moving
content (foreman/stefan). Motion compensation predicts each block from the
*displaced* matching block in the previous frame: per 16x16 block, search a
+/-S-pixel window for the min-SAD displacement, code (motion vector + residual).
Static blocks get MV 0 (same as frame-delta); moving blocks track the motion, so
the residual stays small.

Pipeline (luma, lossless, exactly reversible): frame 0 intra (JXL); each later
frame -> per-block MV + residual; residual stream and MV stream entropy-coded by
our native ctxcoder. Compared against intra-only JXL and the plain frame-delta.
Reconstruction is verified bit-exact. .y4m parsed with numpy.

Usage: python3 scripts/video_mc_benchmark.py [n_frames] [block] [search]
"""
import glob
import sys

import numpy as np
import imagecodecs as ic

from compressor import ctxcoder

NF = int(sys.argv[1]) if len(sys.argv) > 1 else 60
B = int(sys.argv[2]) if len(sys.argv) > 2 else 16
S = int(sys.argv[3]) if len(sys.argv) > 3 else 8


def read_y4m_luma(path, n_frames):
    raw = open(path, "rb").read()
    nl = raw.index(b"\n")
    W = H = None
    for tok in raw[:nl].decode("ascii").split():
        if tok[0] == "W":
            W = int(tok[1:])
        elif tok[0] == "H":
            H = int(tok[1:])
    ysize = W * H
    framesize = ysize + 2 * ((W // 2) * (H // 2))
    pos, frames = nl + 1, []
    while pos < len(raw) and len(frames) < n_frames:
        pos = raw.index(b"\n", pos) + 1
        frames.append(np.frombuffer(raw[pos:pos + ysize], dtype=np.uint8).reshape(H, W))
        pos += framesize
    return np.stack(frames)


def shift_clamp(a, dy, dx):
    H, W = a.shape
    ys = np.clip(np.arange(H) + dy, 0, H - 1)
    xs = np.clip(np.arange(W) + dx, 0, W - 1)
    return a[ys][:, xs]


def motion_estimate(prev, curr, B, S):
    """Per-block best displacement (min SAD) in +/-S; returns (dy,dx) block maps."""
    H, W = curr.shape
    nby, nbx = H // B, W // B
    p, c = prev.astype(np.int16), curr.astype(np.int16)
    best = np.full((nby, nbx), 1 << 30, dtype=np.int64)
    bdy = np.zeros((nby, nbx), dtype=np.int64)
    bdx = np.zeros((nby, nbx), dtype=np.int64)
    for dy in range(-S, S + 1):
        for dx in range(-S, S + 1):
            sad = np.abs(c - shift_clamp(p, dy, dx)).reshape(nby, B, nbx, B).sum((1, 3))
            m = sad < best                      # strict: ties keep smaller |MV| (0 first)
            best = np.where(m, sad, best)
            bdy = np.where(m, dy, bdy)
            bdx = np.where(m, dx, bdx)
    return bdy, bdx


def predict(prev, bdy, bdx, B):
    H, W = prev.shape
    yy, xx = np.meshgrid(np.arange(H), np.arange(W), indexing="ij")
    dymap = np.repeat(np.repeat(bdy, B, 0), B, 1)
    dxmap = np.repeat(np.repeat(bdx, B, 0), B, 1)
    sy = np.clip(yy + dymap, 0, H - 1)
    sx = np.clip(xx + dxmap, 0, W - 1)
    return prev[sy, sx]


def main():
    print(f"block={B} search=+/-{S}   frames={NF}")
    print(f"{'clip':<14}{'intra-JXL':>11}{'frame-delta':>13}{'motion-comp':>13}{'verdict':>22}")
    print("-" * 74)
    for path in sorted(glob.glob("/home/user/sci_data/video/*.y4m")):
        name = path.split("/")[-1].replace(".y4m", "")
        Y = read_y4m_luma(path, NF)
        T, H, W = Y.shape

        intra = sum(len(ic.jpegxl_encode(np.ascontiguousarray(Y[t]), lossless=True))
                    for t in range(T))

        # plain frame-delta (signed) -> ctx
        d = np.empty(Y.shape, dtype=np.int64)
        d[0] = Y[0]
        d[1:] = Y[1:].astype(np.int64) - Y[:-1].astype(np.int64)
        fdelta = len(ctxcoder.encode(d.reshape(-1)))

        # motion compensation
        residuals = [Y[0].astype(np.int64)]      # frame 0 coded as-is in the residual stream
        mvs = []
        recon = [Y[0].copy()]
        for t in range(1, T):
            prev = recon[-1]                     # reconstructed prev (== Y[t-1], lossless)
            bdy, bdx = motion_estimate(prev, Y[t], B, S)
            pred = predict(prev, bdy, bdx, B)
            res = Y[t].astype(np.int64) - pred.astype(np.int64)
            residuals.append(res)
            mvs.append(np.stack([bdy.reshape(-1), bdx.reshape(-1)]).reshape(-1))
            recon.append(((pred.astype(np.int64) + res) & 0xFF).astype(np.uint8))
        assert np.array_equal(np.stack(recon), Y), f"MC round-trip FAILED {name}"
        res_stream = np.concatenate([r.reshape(-1) for r in residuals])
        mv_stream = np.concatenate(mvs) if mvs else np.zeros(0, dtype=np.int64)
        # entropy: frame0 via JXL (fair intra), residuals + MVs via ctx
        mc = (len(ic.jpegxl_encode(np.ascontiguousarray(Y[0]), lossless=True))
              + len(ctxcoder.encode(np.concatenate([r.reshape(-1) for r in residuals[1:]])
                                    if T > 1 else np.zeros(0, dtype=np.int64)))
              + len(ctxcoder.encode(mv_stream)))

        best = min(fdelta, mc)
        verdict = "MC beats intra" if mc < intra else (
            "frame-delta wins" if fdelta < intra else "intra wins")
        gain = (intra - best) / intra * 100
        print(f"{name:<14}{intra/1e6:>10.3f}M{fdelta/1e6:>12.3f}M{mc/1e6:>12.3f}M"
              f"{verdict + f' {gain:+.0f}%':>22}")


if __name__ == "__main__":
    main()
