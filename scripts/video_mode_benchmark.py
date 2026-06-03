"""Per-block intra/inter mode selection on top of motion compensation.

Pure MC predicts every block from the previous frame; that fails on occlusion /
newly-revealed content (no good past match), where the residual is large. Mode
selection lets each 16x16 block pick the cheaper of:
  * INTER: residual against the motion-compensated previous-frame block.
  * INTRA: residual against a causal within-frame predictor (the pixel above,
    i.e. the PNG "Up" filter) — reconstructible row-by-row, fully verifiable.
The per-block mode (1 bit), motion vectors (inter blocks only) and the chosen
residual are all entropy-coded by our native ctxcoder; frame 0 is intra (JXL).

Compared against intra-only JXL and all-inter MC. Reconstruction is verified
bit-exact. .y4m parsed with numpy.

Usage: python3 scripts/video_mode_benchmark.py [n_frames] [block] [search]
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
    H, W = curr.shape
    nby, nbx = H // B, W // B
    p, c = prev.astype(np.int16), curr.astype(np.int16)
    best = np.full((nby, nbx), 1 << 30, dtype=np.int64)
    bdy = np.zeros((nby, nbx), dtype=np.int64)
    bdx = np.zeros((nby, nbx), dtype=np.int64)
    for dy in range(-S, S + 1):
        for dx in range(-S, S + 1):
            sad = np.abs(c - shift_clamp(p, dy, dx)).reshape(nby, B, nbx, B).sum((1, 3))
            m = sad < best
            best = np.where(m, sad, best)
            bdy = np.where(m, dy, bdy)
            bdx = np.where(m, dx, bdx)
    return bdy, bdx


def predict_mc(prev, bdy, bdx, B):
    H, W = prev.shape
    yy, xx = np.meshgrid(np.arange(H), np.arange(W), indexing="ij")
    sy = np.clip(yy + np.repeat(np.repeat(bdy, B, 0), B, 1), 0, H - 1)
    sx = np.clip(xx + np.repeat(np.repeat(bdx, B, 0), B, 1), 0, W - 1)
    return prev[sy, sx].astype(np.int64)


def block_cost(res, B):
    """Per-block proxy for coded bits: sum of zigzag bit-lengths."""
    nby, nbx = res.shape[0] // B, res.shape[1] // B
    zz = np.where(res >= 0, 2 * res, -2 * res - 1).astype(np.float64)
    bl = np.zeros_like(zz)
    pos = zz > 0
    bl[pos] = np.frexp(zz[pos])[1]          # = bit_length for integers
    return bl.reshape(nby, B, nbx, B).sum((1, 3))


def main():
    print(f"block={B} search=+/-{S}   frames={NF}")
    print(f"{'clip':<14}{'intra-JXL':>11}{'MC':>11}{'MC+mode':>11}{'intra%':>8}{'verdict':>20}")
    print("-" * 76)
    for path in sorted(glob.glob("/home/user/sci_data/video/*.y4m")):
        name = path.split("/")[-1].replace(".y4m", "")
        Y = read_y4m_luma(path, NF).astype(np.int64)
        T, H, W = Y.shape
        nby, nbx = H // B, W // B

        intra = sum(len(ic.jpegxl_encode(np.ascontiguousarray(Y[t].astype(np.uint8)),
                                         lossless=True)) for t in range(T))

        mc_res, mc_mv = [], []                 # all-inter MC
        md_res, md_mode, md_mv = [], [], []    # mode-selected
        recon_prev = Y[0]
        intra_blocks = total_blocks = 0
        for t in range(1, T):
            bdy, bdx = motion_estimate(recon_prev, Y[t], B, S)
            mc_pred = predict_mc(recon_prev, bdy, bdx, B)
            inter_res = Y[t] - mc_pred

            vp = np.empty_like(Y[t]); vp[0] = 128; vp[1:] = Y[t][:-1]
            intra_res = Y[t] - vp

            use_intra = block_cost(intra_res, B) < block_cost(inter_res, B)   # (nby,nbx)
            colmask = lambda br: np.repeat(use_intra[br], B)                   # noqa: E731

            # mode-selected residual frame (vectorised)
            intra_mask = np.repeat(np.repeat(use_intra, B, 0), B, 1)
            sel = np.where(intra_mask, intra_res, inter_res)
            # verify reconstruction (row-causal: intra needs the reconstructed row above)
            rec = np.empty_like(Y[t])
            for y in range(H):
                ic_ = colmask(y // B)
                above = rec[y - 1] if y > 0 else np.full(W, 128, dtype=np.int64)
                pred_row = np.where(ic_, above, mc_pred[y])
                rec[y] = pred_row + sel[y]
            assert np.array_equal(rec, Y[t]), f"mode round-trip FAILED {name} f{t}"

            md_res.append(sel)
            md_mode.append(use_intra.reshape(-1).astype(np.int64))
            inter = ~use_intra
            md_mv.append(np.concatenate([bdy[inter], bdx[inter]]))
            intra_blocks += int(use_intra.sum()); total_blocks += use_intra.size

            mc_res.append(inter_res)
            mc_mv.append(np.concatenate([bdy.reshape(-1), bdx.reshape(-1)]))
            recon_prev = Y[t]

        f0 = len(ic.jpegxl_encode(np.ascontiguousarray(Y[0].astype(np.uint8)), lossless=True))

        def cat(xs):
            return np.concatenate([x.reshape(-1) for x in xs]) if xs else np.zeros(0, np.int64)

        mc = f0 + len(ctxcoder.encode(cat(mc_res))) + len(ctxcoder.encode(cat(mc_mv)))
        mcmode = (f0 + len(ctxcoder.encode(cat(md_res)))
                  + len(ctxcoder.encode(cat(md_mode))) + len(ctxcoder.encode(cat(md_mv))))

        ip = 100.0 * intra_blocks / max(1, total_blocks)
        verdict = "MC+mode beats intra" if mcmode < intra else "intra wins"
        gain = (intra - mcmode) / intra * 100
        print(f"{name:<14}{intra/1e6:>10.3f}M{mc/1e6:>10.3f}M{mcmode/1e6:>10.3f}M"
              f"{ip:>7.0f}%{verdict + f' {gain:+.0f}%':>20}")


if __name__ == "__main__":
    main()
