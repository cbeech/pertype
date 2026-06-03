"""Per-block SKIP mode on top of half-pel MC + mode selection (MED intra).

In a lossless codec a block can only be "skipped" (no residual coded at all) when
it is bit-identical to its prediction. The cheapest such prediction is the
co-located previous-frame block (MV 0), so SKIP = "this block equals the previous
frame exactly" — common in static backgrounds / screen content / surveillance. A
skip block costs only its mode flag: no motion vector, no residual.

Three modes per 16x16 block, chosen per block:
  * SKIP  : block == co-located previous block (zero residual, no MV).
  * INTER : half-pel motion-compensated residual (+ MV).
  * INTRA : causal MED residual.
Mode (0/1/2), motion vectors (inter only) and residuals (inter+intra only) are
ctxcoder-coded; skip blocks contribute nothing but the flag. Reconstruction
verified bit-exact. Reports half-pel without vs with skip, and intra-only JXL.

Usage: python3 scripts/video_skip_benchmark.py [n_frames] [block] [search]
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
    P = prev.astype(np.int64)
    return (P, (P + _right(P) + 1) // 2, (P + _down(P) + 1) // 2,
            (P + _right(P) + _down(P) + _right(_down(P)) + 2) // 4)


def predict_subpel(planes, mvy, mvx, B):
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
    nby, nbx = bdy.shape
    best = np.full((nby, nbx), 1 << 30, dtype=np.int64)
    bmvy, bmvx = 2 * bdy.copy(), 2 * bdx.copy()
    for ddy in (0, -1, 1):
        for ddx in (0, -1, 1):
            mvy, mvx = 2 * bdy + ddy, 2 * bdx + ddx
            sad = np.abs(curr - predict_subpel(planes, mvy, mvx, B)).reshape(
                nby, B, nbx, B).sum((1, 3))
            m = sad < best
            best = np.where(m, sad, best)
            bmvy = np.where(m, mvy, bmvy)
            bmvx = np.where(m, mvx, bmvx)
    return bmvy, bmvx


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


def _blocks_of(frame, skip_block_mask, B):
    """Flatten the non-skip blocks of a frame, in block raster order."""
    nby, nbx = frame.shape[0] // B, frame.shape[1] // B
    rb = frame.reshape(nby, B, nbx, B).transpose(0, 2, 1, 3).reshape(nby * nbx, B * B)
    return rb[~skip_block_mask.reshape(-1)].reshape(-1)


def run(Y, skip):
    T, H, W = Y.shape
    res_acc, mode_acc, mv_acc = [], [], []
    skip_n = intra_n = total_blocks = 0
    recon_prev = Y[0]
    for t in range(1, T):
        bdy, bdx = motion_estimate(recon_prev, Y[t], B, S)
        planes = halfpel_planes(recon_prev)
        mvy, mvx = refine_halfpel(planes, Y[t], bdy, bdx, B)
        mc_pred = predict_subpel(planes, mvy, mvx, B)
        inter_res = Y[t] - mc_pred
        intra_res = Y[t] - med_predict(Y[t])

        use_intra = block_cost(intra_res, B) < block_cost(inter_res, B)
        if skip:
            diff = (Y[t] - recon_prev).reshape(Y[t].shape[0] // B, B, W // B, B)
            skip_block = (diff == 0).all((1, 3))            # exact co-located match
        else:
            skip_block = np.zeros_like(use_intra, dtype=bool)
        intra_block = use_intra & ~skip_block
        inter_block = ~use_intra & ~skip_block

        sel = np.where(np.repeat(np.repeat(intra_block, B, 0), B, 1), intra_res, inter_res)
        res_acc.append(_blocks_of(sel, skip_block, B))
        mode = np.zeros_like(use_intra, dtype=np.int64)
        mode[intra_block] = 1
        mode[skip_block] = 2
        mode_acc.append(mode.reshape(-1))
        mv_acc.append(np.concatenate([mvy[inter_block], mvx[inter_block]]))

        # reconstruct: skip -> co-located prev; inter -> mc+res; intra -> causal MED
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

        skip_n += int(skip_block.sum()); intra_n += int(intra_block.sum())
        total_blocks += skip_block.size
        recon_prev = Y[t]

    f0 = len(ic.jpegxl_encode(np.ascontiguousarray(Y[0].astype(np.uint8)), lossless=True))
    total = (f0 + len(ctxcoder.encode(_cat(res_acc)))
             + len(ctxcoder.encode(_cat(mode_acc))) + len(ctxcoder.encode(_cat(mv_acc))))
    return total, 100.0 * skip_n / max(1, total_blocks), 100.0 * intra_n / max(1, total_blocks)


def main():
    print(f"block={B} search=+/-{S}   frames={NF}")
    print(f"{'clip':<14}{'intra-JXL':>11}{'half-pel':>11}{'+skip':>11}{'skip gain':>11}  modes")
    print("-" * 72)
    for path in sorted(glob.glob("/home/user/sci_data/video/*.y4m")):
        name = path.split("/")[-1].replace(".y4m", "")
        Y = read_y4m_luma(path, NF).astype(np.int64)
        intra = sum(len(ic.jpegxl_encode(np.ascontiguousarray(Y[t].astype(np.uint8)),
                                         lossless=True)) for t in range(len(Y)))
        base, _, _ = run(Y, skip=False)
        sk, skp, inp = run(Y, skip=True)
        print(f"{name:<14}{intra/1e6:>10.3f}M{base/1e6:>10.3f}M{sk/1e6:>10.3f}M"
              f"{(base - sk) / base * 100:>+10.1f}%  (intra {(intra - sk) / intra * 100:+.0f}%, "
              f"{skp:.0f}% skip {inp:.0f}% intra)")


if __name__ == "__main__":
    main()
