"""Quarter-pixel motion vectors, on the full pipeline (MC + per-block
SKIP/INTER/INTRA mode selection with MED intra).

Generalises the sub-pel predictor to a single bilinear sampler in quarter-pel
units: a displacement mvy/4, mvx/4 with mvy = 4*iy + ry (ry in 0..3) blends the
four surrounding integer pixels with 1/16-resolution weights (integer and
half-pel fall out as special cases). Search refines integer -> half-pel -> quarter
-pel (9 candidates each). Losslessness is unaffected (the predictor is a
deterministic function of the reconstructed previous frame); reconstruction
verified bit-exact.

Reports half-pel vs quarter-pel (both with mode selection + skip) vs intra-only JXL.
Usage: python3 scripts/video_qpel_benchmark.py [n_frames] [block] [search]
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


def predict_qpel(P, mvy, mvx, B, yy, xx):
    """Bilinear sampler at quarter-pel MV (units of 1/4 px). mvy = 4*iy + ry,
    ry in 0..3; weights are (4-ry)/4 x (4-rx)/4 over the 4 integer neighbours."""
    H, W = P.shape
    iy, ry = mvy >> 2, mvy & 3
    ix, rx = mvx >> 2, mvx & 3
    iym = np.repeat(np.repeat(iy, B, 0), B, 1)
    ixm = np.repeat(np.repeat(ix, B, 0), B, 1)
    rym = np.repeat(np.repeat(ry, B, 0), B, 1)
    rxm = np.repeat(np.repeat(rx, B, 0), B, 1)
    Y = np.clip(yy + iym, 0, H - 1); Yp = np.clip(yy + iym + 1, 0, H - 1)
    X = np.clip(xx + ixm, 0, W - 1); Xp = np.clip(xx + ixm + 1, 0, W - 1)
    w00 = (4 - rym) * (4 - rxm); w01 = (4 - rym) * rxm
    w10 = rym * (4 - rxm); w11 = rym * rxm
    return (w00 * P[Y, X] + w01 * P[Y, Xp] + w10 * P[Yp, X] + w11 * P[Yp, Xp] + 8) >> 4


def refine(prev, curr, bdy, bdx, B, quarter, yy, xx):
    """Integer MV -> half-pel -> (optionally) quarter-pel, by min block SAD."""
    nby, nbx = bdy.shape

    def search(base_y, base_x, steps):
        best = np.full((nby, nbx), 1 << 30, dtype=np.int64)
        by, bx = base_y.copy(), base_x.copy()
        for ddy in steps:
            for ddx in steps:
                mvy, mvx = base_y + ddy, base_x + ddx
                sad = np.abs(curr - predict_qpel(prev, mvy, mvx, B, yy, xx)).reshape(
                    nby, B, nbx, B).sum((1, 3))
                m = sad < best
                best = np.where(m, sad, best)
                by = np.where(m, mvy, by)
                bx = np.where(m, mvx, bx)
        return by, bx

    hy, hx = search(4 * bdy, 4 * bdx, (0, -2, 2))      # half-pel (centre first)
    if quarter:
        return search(hy, hx, (0, -1, 1))              # quarter-pel
    return hy, hx


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


def _blocks_of(frame, skip_block, B):
    nby, nbx = frame.shape[0] // B, frame.shape[1] // B
    rb = frame.reshape(nby, B, nbx, B).transpose(0, 2, 1, 3).reshape(nby * nbx, B * B)
    return rb[~skip_block.reshape(-1)].reshape(-1)


def run(Y, quarter):
    T, H, W = Y.shape
    yy, xx = np.meshgrid(np.arange(H), np.arange(W), indexing="ij")
    res_acc, mode_acc, mv_acc = [], [], []
    recon_prev = Y[0]
    for t in range(1, T):
        bdy, bdx = motion_estimate(recon_prev, Y[t], B, S)
        mvy, mvx = refine(recon_prev, Y[t], bdy, bdx, B, quarter, yy, xx)
        mc_pred = predict_qpel(recon_prev, mvy, mvx, B, yy, xx)
        inter_res = Y[t] - mc_pred
        intra_res = Y[t] - med_predict(Y[t])

        use_intra = block_cost(intra_res, B) < block_cost(inter_res, B)
        diff = (Y[t] - recon_prev).reshape(H // B, B, W // B, B)
        skip_block = (diff == 0).all((1, 3))
        intra_block = use_intra & ~skip_block
        inter_block = ~use_intra & ~skip_block

        sel = np.where(np.repeat(np.repeat(intra_block, B, 0), B, 1), intra_res, inter_res)
        res_acc.append(_blocks_of(sel, skip_block, B))
        mode = np.zeros_like(use_intra, dtype=np.int64)
        mode[intra_block] = 1; mode[skip_block] = 2
        mode_acc.append(mode.reshape(-1))
        mv_acc.append(np.concatenate([mvy[inter_block], mvx[inter_block]]))

        skip_px = np.repeat(np.repeat(skip_block, B, 0), B, 1)
        intra_px = np.repeat(np.repeat(intra_block, B, 0), B, 1)
        rec = np.where(skip_px, recon_prev,
                       np.where(intra_px, np.int64(-1 << 30), mc_pred + inter_res))
        for y, x in np.argwhere(intra_px).tolist():
            a = rec[y, x - 1] if x > 0 else (rec[y - 1, x] if y > 0 else 128)
            b = rec[y - 1, x] if y > 0 else a
            c = rec[y - 1, x - 1] if (x > 0 and y > 0) else b
            mx = a if a > b else b
            mn = a if a < b else b
            pred = mn if c >= mx else (mx if c <= mn else a + b - c)
            rec[y, x] = pred + intra_res[y, x]
        assert np.array_equal(rec, Y[t]), "round-trip FAILED"
        recon_prev = Y[t]

    f0 = len(ic.jpegxl_encode(np.ascontiguousarray(Y[0].astype(np.uint8)), lossless=True))
    return (f0 + len(ctxcoder.encode(_cat(res_acc)))
            + len(ctxcoder.encode(_cat(mode_acc))) + len(ctxcoder.encode(_cat(mv_acc))))


def main():
    print(f"block={B} search=+/-{S}   frames={NF}")
    print(f"{'clip':<14}{'intra-JXL':>11}{'half-pel':>11}{'quarter-pel':>13}{'qpel gain':>11}")
    print("-" * 62)
    for path in sorted(glob.glob("/home/user/sci_data/video/*.y4m")):
        name = path.split("/")[-1].replace(".y4m", "")
        Y = read_y4m_luma(path, NF).astype(np.int64)
        intra = sum(len(ic.jpegxl_encode(np.ascontiguousarray(Y[t].astype(np.uint8)),
                                         lossless=True)) for t in range(len(Y)))
        h = run(Y, quarter=False)
        q = run(Y, quarter=True)
        print(f"{name:<14}{intra/1e6:>10.3f}M{h/1e6:>10.3f}M{q/1e6:>12.3f}M"
              f"{(h - q) / h * 100:>+10.1f}%   (intra {(intra - q) / intra * 100:+.0f}%)")


if __name__ == "__main__":
    main()
