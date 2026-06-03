"""Joint luma+chroma coding: derive chroma MVs from luma (shared mode + MV).

The independent per-plane coder searched chroma motion separately and coded
chroma MVs and modes — overhead that doesn't pay on smooth low-energy chroma. A
real codec makes ONE decision per block for all planes: one mode (skip/inter/
intra) and one luma MV; chroma inherits the mode and a MV derived from the luma
MV scaled by the 4:2:0 subsampling (chroma_mv = round(luma_mv / 2), in quarter-
chroma-pel). A 16x16 luma block maps to co-located 8x8 chroma blocks, so the
block grids coincide.

Per block: SKIP iff ALL planes are bit-identical to the previous frame; otherwise
the cheaper (joint cost over Y+U+V) of INTER (sub-pel MC, luma MV + derived chroma
MV) or INTRA (MED per plane). Only the luma MV is coded. Reuses the quarter-pel
pipeline pieces from video_qpel_benchmark. Every plane round-trip verified.

Reports intra-only JXL, independent per-plane, and joint. Usage:
python3 scripts/video_joint_benchmark.py [n_frames]
"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import imagecodecs as ic

from compressor import ctxcoder
import video_qpel_benchmark as q

NF = int(sys.argv[1]) if len(sys.argv) > 1 else 60
S = 8


def read_y4m_yuv(path, n):
    raw = open(path, "rb").read()
    nl = raw.index(b"\n")
    W = H = None
    for tok in raw[:nl].decode("ascii").split():
        if tok[0] == "W":
            W = int(tok[1:])
        elif tok[0] == "H":
            H = int(tok[1:])
    ys, cs = W * H, (W // 2) * (H // 2)
    fs = ys + 2 * cs
    pos, Ys, Us, Vs = nl + 1, [], [], []
    while pos < len(raw) and len(Ys) < n:
        pos = raw.index(b"\n", pos) + 1
        Ys.append(np.frombuffer(raw[pos:pos + ys], np.uint8).reshape(H, W))
        Us.append(np.frombuffer(raw[pos + ys:pos + ys + cs], np.uint8).reshape(H // 2, W // 2))
        Vs.append(np.frombuffer(raw[pos + ys + cs:pos + ys + 2 * cs], np.uint8).reshape(H // 2, W // 2))
        pos += fs
    return (np.stack(Ys).astype(np.int64), np.stack(Us).astype(np.int64),
            np.stack(Vs).astype(np.int64))


def intra_jxl(stack):
    return sum(len(ic.jpegxl_encode(np.ascontiguousarray(stack[t].astype(np.uint8)),
                                    lossless=True)) for t in range(len(stack)))


def recon_plane(cur, prev, mc, intra_res, skip_block, intra_block, B):
    """skip -> co-located prev; inter -> mc + residual (== cur); intra -> causal
    MED. Sentinel init on intra pixels so the round-trip exercises the MED chain."""
    skip_px = np.repeat(np.repeat(skip_block, B, 0), B, 1)
    intra_px = np.repeat(np.repeat(intra_block, B, 0), B, 1)
    rec = np.where(skip_px, prev, np.where(intra_px, np.int64(-1 << 30), cur))
    for y, x in np.argwhere(intra_px).tolist():
        a = rec[y, x - 1] if x > 0 else (rec[y - 1, x] if y > 0 else 128)
        b = rec[y - 1, x] if y > 0 else a
        c = rec[y - 1, x - 1] if (x > 0 and y > 0) else b
        mx = a if a > b else b
        mn = a if a < b else b
        pred = mn if c >= mx else (mx if c <= mn else a + b - c)
        rec[y, x] = pred + intra_res[y, x]
    assert np.array_equal(rec, cur), "round-trip FAILED"
    return rec


def run_joint(Y, U, V):
    T, H, W = Y.shape
    Bc = 8
    nby, nbx = H // 16, W // 16
    yy_l, xx_l = np.meshgrid(np.arange(H), np.arange(W), indexing="ij")
    yy_c, xx_c = np.meshgrid(np.arange(H // 2), np.arange(W // 2), indexing="ij")
    rl, ru, rv, mode_acc, mv_acc = [], [], [], [], []
    py, pu, pv = Y[0], U[0], V[0]
    for t in range(1, T):
        bdy, bdx = q.motion_estimate(py, Y[t], 16, S)
        mvy, mvx = q.refine(py, Y[t], bdy, bdx, 16, True, yy_l, xx_l)   # quarter-pel luma MV
        cmy = np.round(mvy / 2).astype(np.int64)                        # derived chroma MV
        cmx = np.round(mvx / 2).astype(np.int64)

        mc_y = q.predict_qpel(py, mvy, mvx, 16, yy_l, xx_l)
        mc_u = q.predict_qpel(pu, cmy, cmx, Bc, yy_c, xx_c)
        mc_v = q.predict_qpel(pv, cmy, cmx, Bc, yy_c, xx_c)
        ai_y, ai_u, ai_v = (Y[t] - q.med_predict(Y[t]),
                            U[t] - q.med_predict(U[t]), V[t] - q.med_predict(V[t]))

        cost_inter = (q.block_cost(Y[t] - mc_y, 16) + q.block_cost(U[t] - mc_u, Bc)
                      + q.block_cost(V[t] - mc_v, Bc))
        cost_intra = q.block_cost(ai_y, 16) + q.block_cost(ai_u, Bc) + q.block_cost(ai_v, Bc)
        use_intra = cost_intra < cost_inter

        sk_y = ((Y[t] - py).reshape(nby, 16, nbx, 16) == 0).all((1, 3))
        sk_u = ((U[t] - pu).reshape(nby, Bc, nbx, Bc) == 0).all((1, 3))
        sk_v = ((V[t] - pv).reshape(nby, Bc, nbx, Bc) == 0).all((1, 3))
        skip_block = sk_y & sk_u & sk_v
        intra_block = use_intra & ~skip_block
        inter_block = ~use_intra & ~skip_block

        im16 = np.repeat(np.repeat(intra_block, 16, 0), 16, 1)
        im8 = np.repeat(np.repeat(intra_block, Bc, 0), Bc, 1)
        rl.append(q._blocks_of(np.where(im16, ai_y, Y[t] - mc_y), skip_block, 16))
        ru.append(q._blocks_of(np.where(im8, ai_u, U[t] - mc_u), skip_block, Bc))
        rv.append(q._blocks_of(np.where(im8, ai_v, V[t] - mc_v), skip_block, Bc))
        mode = np.zeros_like(use_intra, dtype=np.int64)
        mode[intra_block] = 1; mode[skip_block] = 2
        mode_acc.append(mode.reshape(-1))
        mv_acc.append(np.concatenate([mvy[inter_block], mvx[inter_block]]))   # luma MV only

        py = recon_plane(Y[t], py, mc_y, ai_y, skip_block, intra_block, 16)
        pu = recon_plane(U[t], pu, mc_u, ai_u, skip_block, intra_block, Bc)
        pv = recon_plane(V[t], pv, mc_v, ai_v, skip_block, intra_block, Bc)

    f0 = sum(intra_jxl(p[:1]) for p in (Y, U, V))
    streams = [q._cat(rl), q._cat(ru), q._cat(rv), q._cat(mode_acc), q._cat(mv_acc)]
    return f0 + sum(len(ctxcoder.encode(s)) for s in streams)


def main():
    print(f"frames={NF}")
    print(f"{'clip':<12}{'intra-JXL':>11}{'independent':>13}{'joint':>11}"
          f"{'joint-vs-indep':>16}{'vs intra':>10}")
    print("-" * 74)
    for path in sorted(glob.glob("/home/user/sci_data/video/*.y4m")):
        name = path.split("/")[-1].replace(".y4m", "")
        Y, U, V = read_y4m_yuv(path, NF)
        intra = intra_jxl(Y) + intra_jxl(U) + intra_jxl(V)
        indep = q.run(Y, True) + q.run(U, True) + q.run(V, True)
        joint = run_joint(Y, U, V)
        print(f"{name:<12}{intra/1e6:>10.3f}M{indep/1e6:>12.3f}M{joint/1e6:>10.3f}M"
              f"{(indep - joint) / indep * 100:>+15.1f}%{(intra - joint) / intra * 100:>+9.0f}%")


if __name__ == "__main__":
    main()
