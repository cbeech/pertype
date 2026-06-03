"""Half-pixel motion vectors on top of MC + per-block mode selection (MED intra).

Real motion rarely lands on integer pixels, so an integer-only MV leaves a
residual that a half-pel-interpolated prediction removes. After the integer
search we refine each block over the 9 half-pel positions around the best
integer MV (bilinear interpolation of the previous frame), keeping the min-SAD
one. MVs are coded in half-pel units. Everything else is unchanged: per-block
INTER (sub-pel MC) vs INTRA (causal MED), entropy via ctxcoder, frame 0 intra
(JXL). Losslessness is unaffected — the predictor is a deterministic function of
the reconstructed previous frame. Reconstruction verified bit-exact.

Reports integer-MV vs half-pel-MV (both with mode selection) vs intra-only JXL.
Usage: python3 scripts/video_subpel_benchmark.py [n_frames] [block] [search]
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


def _right(A):
    return np.concatenate([A[:, 1:], A[:, -1:]], axis=1)


def _down(A):
    return np.concatenate([A[1:, :], A[-1:, :]], axis=0)


def halfpel_planes(prev):
    """Four reference planes: integer, horizontal/vertical/diagonal half-pel
    (bilinear, rounded). Edge samples clamp."""
    P = prev.astype(np.int64)
    return (P,
            (P + _right(P) + 1) // 2,
            (P + _down(P) + 1) // 2,
            (P + _right(P) + _down(P) + _right(_down(P)) + 2) // 4)


def predict_subpel(planes, mvy, mvx, B):
    """Build the predicted frame from per-block half-pel MVs (units of 1/2 px).
    mvy = 2*iy + ry: iy is the integer shift, ry in {0,1} picks the half-pel plane."""
    ref00, ref01, ref10, ref11 = planes
    H, W = ref00.shape
    iy, ry = mvy >> 1, mvy & 1
    ix, rx = mvx >> 1, mvx & 1
    iym = np.repeat(np.repeat(iy, B, 0), B, 1)
    ixm = np.repeat(np.repeat(ix, B, 0), B, 1)
    plane = np.repeat(np.repeat(ry * 2 + rx, B, 0), B, 1)
    yy, xx = np.meshgrid(np.arange(H), np.arange(W), indexing="ij")
    sy = np.clip(yy + iym, 0, H - 1)
    sx = np.clip(xx + ixm, 0, W - 1)
    return np.where(plane == 0, ref00[sy, sx],
                    np.where(plane == 1, ref01[sy, sx],
                             np.where(plane == 2, ref10[sy, sx], ref11[sy, sx])))


def refine_halfpel(planes, curr, bdy, bdx, B):
    """Refine each block over the 9 half-pel positions around its integer MV."""
    nby, nbx = bdy.shape
    best = np.full((nby, nbx), 1 << 30, dtype=np.int64)
    bmvy = 2 * bdy.copy()
    bmvx = 2 * bdx.copy()
    for ddy in (0, -1, 1):                 # centre first -> ties prefer integer MV
        for ddx in (0, -1, 1):
            mvy, mvx = 2 * bdy + ddy, 2 * bdx + ddx
            sad = np.abs(curr - predict_subpel(planes, mvy, mvx, B)).reshape(
                nby, B, nbx, B).sum((1, 3))
            m = sad < best
            best = np.where(m, sad, best)
            bmvy = np.where(m, mvy, bmvy)
            bmvx = np.where(m, mvx, bmvx)
    return bmvy, bmvx


def predict_mc(prev, bdy, bdx, B):
    H, W = prev.shape
    yy, xx = np.meshgrid(np.arange(H), np.arange(W), indexing="ij")
    sy = np.clip(yy + np.repeat(np.repeat(bdy, B, 0), B, 1), 0, H - 1)
    sx = np.clip(xx + np.repeat(np.repeat(bdx, B, 0), B, 1), 0, W - 1)
    return prev[sy, sx].astype(np.int64)


def med_predict(P):
    a = np.zeros_like(P); a[:, 1:] = P[:, :-1]
    b = np.zeros_like(P); b[1:, :] = P[:-1, :]
    c = np.zeros_like(P); c[1:, 1:] = P[:-1, :-1]
    mx, mn = np.maximum(a, b), np.minimum(a, b)
    pred = np.where(c >= mx, mn, np.where(c <= mn, mx, a + b - c))
    pred[0, 1:] = P[0, :-1]
    pred[1:, 0] = P[:-1, 0]
    pred[0, 0] = 128
    return pred


def block_cost(res, B):
    nby, nbx = res.shape[0] // B, res.shape[1] // B
    zz = np.where(res >= 0, 2 * res, -2 * res - 1).astype(np.float64)
    bl = np.zeros_like(zz)
    pos = zz > 0
    bl[pos] = np.frexp(zz[pos])[1]
    return bl.reshape(nby, B, nbx, B).sum((1, 3))


def _cat(xs):
    return np.concatenate([x.reshape(-1) for x in xs]) if xs else np.zeros(0, np.int64)


def run(Y, half):
    T, H, W = Y.shape
    res_acc, mode_acc, mv_acc = [], [], []
    intra_blocks = total_blocks = 0
    recon_prev = Y[0]
    for t in range(1, T):
        bdy, bdx = motion_estimate(recon_prev, Y[t], B, S)
        if half:
            planes = halfpel_planes(recon_prev)
            mvy, mvx = refine_halfpel(planes, Y[t], bdy, bdx, B)
            mc_pred = predict_subpel(planes, mvy, mvx, B)
        else:
            mvy, mvx = bdy, bdx
            mc_pred = predict_mc(recon_prev, bdy, bdx, B)
        inter_res = Y[t] - mc_pred
        intra_res = Y[t] - med_predict(Y[t])
        use_intra = block_cost(intra_res, B) < block_cost(inter_res, B)
        intra_mask = np.repeat(np.repeat(use_intra, B, 0), B, 1)
        sel = np.where(intra_mask, intra_res, inter_res)

        rec = np.where(intra_mask, np.int64(-1 << 30), mc_pred + inter_res)
        for y, x in np.argwhere(intra_mask).tolist():
            a = rec[y, x - 1] if x > 0 else (rec[y - 1, x] if y > 0 else 128)
            b = rec[y - 1, x] if y > 0 else a
            c = rec[y - 1, x - 1] if (x > 0 and y > 0) else b
            mx = a if a > b else b
            mn = a if a < b else b
            pred = mn if c >= mx else (mx if c <= mn else a + b - c)
            rec[y, x] = pred + intra_res[y, x]
        assert np.array_equal(rec, Y[t]), "round-trip FAILED"

        res_acc.append(sel)
        mode_acc.append(use_intra.reshape(-1).astype(np.int64))
        inter = ~use_intra
        mv_acc.append(np.concatenate([mvy[inter], mvx[inter]]))
        intra_blocks += int(use_intra.sum()); total_blocks += use_intra.size
        recon_prev = Y[t]

    f0 = len(ic.jpegxl_encode(np.ascontiguousarray(Y[0].astype(np.uint8)), lossless=True))
    total = (f0 + len(ctxcoder.encode(_cat(res_acc)))
             + len(ctxcoder.encode(_cat(mode_acc))) + len(ctxcoder.encode(_cat(mv_acc))))
    return total, 100.0 * intra_blocks / max(1, total_blocks)


def main():
    print(f"block={B} search=+/-{S}   frames={NF}")
    print(f"{'clip':<14}{'intra-JXL':>11}{'mode int-MV':>13}{'mode half-pel':>14}{'half gain':>11}")
    print("-" * 64)
    for path in sorted(glob.glob("/home/user/sci_data/video/*.y4m")):
        name = path.split("/")[-1].replace(".y4m", "")
        Y = read_y4m_luma(path, NF).astype(np.int64)
        intra = sum(len(ic.jpegxl_encode(np.ascontiguousarray(Y[t].astype(np.uint8)),
                                         lossless=True)) for t in range(len(Y)))
        ti, _ = run(Y, half=False)
        th, ip = run(Y, half=True)
        print(f"{name:<14}{intra/1e6:>10.3f}M{ti/1e6:>12.3f}M{th/1e6:>13.3f}M"
              f"{(ti - th) / ti * 100:>+10.1f}%   (intra {(intra - th) / intra * 100:+.0f}%, "
              f"{ip:.0f}% intra)")


if __name__ == "__main__":
    main()
