"""Lossless video codec — motion-compensated inter-frame coding.

The real encode/decode path for the pipeline validated in ``scripts/video_*``
(temporal motion compensation with quarter-pel vectors and per-block
SKIP / INTER / INTRA mode selection, MED intra). Unlike the benchmark scripts —
which only *measure* stream sizes — this produces a decodable container and
reconstructs frames from it, byte-exact.

Per 16x16 block, per frame (frame 0 is all-intra):
  * SKIP  : block is bit-identical to the co-located previous block (MV 0); coded
            as just the mode flag.
  * INTER : quarter-pel motion-compensated residual against the previous frame
            (+ a luma motion vector).
  * INTRA : residual against the causal MED (JPEG-LS) predictor in this frame.
The mode field, motion vectors (inter blocks only) and residuals (non-skip
blocks) are entropy-coded by :mod:`pertype.ctxcoder`. Colour is handled by
coding each plane independently (the per-plane winner; see the README).

Single plane: :func:`encode` / :func:`decode` on a ``(T, H, W)`` uint8 array
(H and W multiples of 16). Planar video: :func:`encode_yuv` / :func:`decode_yuv`.
"""
import numpy as np

from pertype import ctxcoder, predictors

_native = None


def _get_native():
    global _native
    if _native is None:
        try:
            from pertype import native as n
            _native = n if n.HAVE_NATIVE else False
        except Exception:
            _native = False
    return _native


MAGIC = b"VID1"
MAGIC_YUV = b"VYUV"
B = 16          # block size
S = 8           # integer motion search radius (pixels) at each pyramid level
R = 4           # full-res integer refinement radius around the coarse vector


# --- prediction / cost primitives -------------------------------------------

def _shift_clamp(a, dy, dx):
    H, W = a.shape
    ys = np.clip(np.arange(H) + dy, 0, H - 1)
    xs = np.clip(np.arange(W) + dx, 0, W - 1)
    return a[ys][:, xs]


def _full_search(prev, curr, radius, blk):
    """Vectorized full integer search: per ``blk``x``blk`` block, the (dy, dx) in
    [-radius, radius]^2 minimising SAD vs ``prev``. All blocks step in lockstep."""
    H, W = curr.shape
    nby, nbx = H // blk, W // blk
    p, c = prev.astype(np.int16), curr.astype(np.int16)
    best = np.full((nby, nbx), 1 << 30, dtype=np.int64)
    bdy = np.zeros((nby, nbx), dtype=np.int64)
    bdx = np.zeros((nby, nbx), dtype=np.int64)
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            sad = np.abs(c - _shift_clamp(p, dy, dx)).reshape(nby, blk, nbx, blk).sum((1, 3))
            m = sad < best
            best = np.where(m, sad, best)
            bdy = np.where(m, dy, bdy)
            bdx = np.where(m, dx, bdx)
    return bdy, bdx, best


def _sad_int(prev, curr, mvy, mvx, yy, xx):
    """Per-block SAD for *per-block* integer MVs (pixels), via a clamped gather —
    so each block can search around its own centre (unlike the lockstep full search)."""
    H, W = prev.shape
    nby, nbx = curr.shape[0] // B, curr.shape[1] // B
    iym = np.repeat(np.repeat(mvy, B, 0), B, 1)
    ixm = np.repeat(np.repeat(mvx, B, 0), B, 1)
    Y = np.clip(yy + iym, 0, H - 1)
    X = np.clip(xx + ixm, 0, W - 1)
    return np.abs(curr - prev[Y, X]).reshape(nby, B, nbx, B).sum((1, 3))


def _motion_estimate(prev, curr, yy, xx):
    """Hierarchical motion search. A coarse full search on the ÷2 box-downsampled
    frame extends the effective range to ~±2S px cheaply (high-motion content moves
    well past ±S at HD); the coarse vector is upscaled and refined per block at full
    resolution over ±R. A final guard prefers the zero vector where it ties or wins,
    so coarse aliasing never makes a block *worse* than the plain co-located match."""
    pc = (prev[0::2, 0::2] + prev[1::2, 0::2] + prev[0::2, 1::2] + prev[1::2, 1::2]) >> 2
    cc = (curr[0::2, 0::2] + curr[1::2, 0::2] + curr[0::2, 1::2] + curr[1::2, 1::2]) >> 2
    cby, cbx, _ = _full_search(pc, cc, S, B // 2)          # coarse: ±S coarse-px = ±2S full
    base_y, base_x = cby * 2, cbx * 2                       # upscale to full-res pixels

    nby, nbx = curr.shape[0] // B, curr.shape[1] // B
    best = np.full((nby, nbx), 1 << 30, dtype=np.int64)
    by = base_y.copy()
    bx = base_x.copy()
    for ddy in range(-R, R + 1):                            # full-res refine around the base
        for ddx in range(-R, R + 1):
            mvy, mvx = base_y + ddy, base_x + ddx
            sad = _sad_int(prev, curr, mvy, mvx, yy, xx)
            m = sad < best
            best = np.where(m, sad, best)
            by = np.where(m, mvy, by)
            bx = np.where(m, mvx, bx)
    sad0 = _sad_int(prev, curr, np.zeros_like(by), np.zeros_like(bx), yy, xx)
    z = sad0 <= best                                        # prefer MV 0 on ties/aliasing
    return np.where(z, 0, by), np.where(z, 0, bx)


def _predict_qpel(P, mvy, mvx, yy, xx):
    """Bilinear sampler at quarter-pel per-block MV (units of 1/4 px)."""
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


def _refine(prev, curr, bdy, bdx, yy, xx):
    """Integer MV -> half-pel -> quarter-pel by min block SAD (centre first)."""
    nby, nbx = bdy.shape

    def search(base_y, base_x, steps):
        best = np.full((nby, nbx), 1 << 30, dtype=np.int64)
        by, bx = base_y.copy(), base_x.copy()
        for ddy in steps:
            for ddx in steps:
                mvy, mvx = base_y + ddy, base_x + ddx
                sad = np.abs(curr - _predict_qpel(prev, mvy, mvx, yy, xx)).reshape(
                    nby, B, nbx, B).sum((1, 3))
                m = sad < best
                best = np.where(m, sad, best)
                by = np.where(m, mvy, by)
                bx = np.where(m, mvx, bx)
        return by, bx

    hy, hx = search(4 * bdy, 4 * bdx, (0, -2, 2))
    return search(hy, hx, (0, -1, 1))


def _med_predict(P):
    # The shared JPEG-LS MED predictor (pertype/predictors.py). Returns P's dtype
    # (uint8) — byte-identical to the old in-line version: the gradient branch a+b-c
    # only fires for c in [min(a,b), max(a,b)], so the result stays in [0, 255] and
    # never overflows, and the origin/edge convention (128, left, up) matches.
    return predictors.med_predict(P.astype(np.int32)).astype(P.dtype)


def _med_fill(rec, intra_px, residual):
    """Reconstruct intra pixels (where ``intra_px``) in raster order via causal
    MED, in place; ``rec`` already holds the non-intra (skip/inter) pixels.
    Native when available (byte-identical), else the pure-Python loop below."""
    nat = _get_native()
    if nat:
        nat.med_fill(rec, intra_px, residual)
        return
    for y, x in np.argwhere(intra_px).tolist():
        a = rec[y, x - 1] if x > 0 else (rec[y - 1, x] if y > 0 else 128)
        b = rec[y - 1, x] if y > 0 else a
        c = rec[y - 1, x - 1] if (x > 0 and y > 0) else b
        mx = a if a > b else b
        mn = a if a < b else b
        pred = mn if c >= mx else (mx if c <= mn else a + b - c)
        rec[y, x] = pred + residual[y, x]


def _block_cost(res):
    nby, nbx = res.shape[0] // B, res.shape[1] // B
    zz = np.where(res >= 0, 2 * res, -2 * res - 1).astype(np.float64)
    bl = np.zeros_like(zz)
    pos = zz > 0
    bl[pos] = np.frexp(zz[pos])[1]
    return bl.reshape(nby, B, nbx, B).sum((1, 3))


# --- container helpers -------------------------------------------------------

def _put(out, blob):
    out += len(blob).to_bytes(4, "big")
    out += blob


def _take(blob, pos):
    n = int.from_bytes(blob[pos:pos + 4], "big")
    return blob[pos + 4:pos + 4 + n], pos + 4 + n


def _to_blocks(frame, keep):
    """Non-``skip`` blocks of ``frame`` flattened in block raster order."""
    nby, nbx = frame.shape[0] // B, frame.shape[1] // B
    rb = frame.reshape(nby, B, nbx, B).transpose(0, 2, 1, 3).reshape(nby * nbx, B * B)
    return rb[keep.reshape(-1)].reshape(-1)


def _from_blocks(values, keep, H, W):
    nby, nbx = H // B, W // B
    rb = np.zeros((nby * nbx, B * B), dtype=np.int64)
    rb[keep.reshape(-1)] = values.reshape(-1, B * B)
    return rb.reshape(nby, nbx, B, B).transpose(0, 2, 1, 3).reshape(H, W)


# --- single-plane encode / decode -------------------------------------------

def _choose_modes(prev, cur, yy, xx, nby, nbx):
    """Per-block mode decision for one inter frame: returns (mode, mvy, mvx, sel,
    skip_block, inter_block). ``mode`` is 0=inter / 1=intra / 2=skip; ``sel`` is the
    chosen residual (inter MC residual or intra MED residual) at every pixel. Shared
    by :func:`encode` and :func:`mode_stats` so the two never drift."""
    bdy, bdx = _motion_estimate(prev, cur, yy, xx)
    mvy, mvx = _refine(prev, cur, bdy, bdx, yy, xx)
    mc = _predict_qpel(prev, mvy, mvx, yy, xx)
    inter_res = cur - mc
    intra_res = cur - _med_predict(cur)

    use_intra = _block_cost(intra_res) < _block_cost(inter_res)
    skip_block = ((cur - prev).reshape(nby, B, nbx, B) == 0).all((1, 3))
    intra_block = use_intra & ~skip_block
    inter_block = ~use_intra & ~skip_block

    mode = np.zeros((nby, nbx), dtype=np.int64)
    mode[intra_block] = 1; mode[skip_block] = 2
    sel = np.where(np.repeat(np.repeat(intra_block, B, 0), B, 1), intra_res, inter_res)
    return mode, mvy, mvx, sel, skip_block, inter_block


def mode_stats(frames):
    """Block-mode mix over the inter frames (frame 0 excluded): a dict of skip /
    inter / intra counts + fractions. This is *why* a clip wins or loses — inter/skip
    dominate where our temporal coding beats intra-only FFV1 (animation, static
    scenes); intra dominates on high-motion live action (where the plain-MED intra
    path is the limiter, not motion search)."""
    F = np.asarray(frames).astype(np.int64)
    T, H, W = F.shape
    yy, xx = np.meshgrid(np.arange(H), np.arange(W), indexing="ij")
    nby, nbx = H // B, W // B
    skip = inter = intra = 0
    for t in range(1, T):
        mode, *_ = _choose_modes(F[t - 1], F[t], yy, xx, nby, nbx)
        skip += int((mode == 2).sum())
        intra += int((mode == 1).sum())
        inter += int((mode == 0).sum())
    tot = max(1, skip + inter + intra)
    return {"skip": skip, "inter": inter, "intra": intra,
            "skip_pct": 100 * skip / tot, "inter_pct": 100 * inter / tot,
            "intra_pct": 100 * intra / tot}


def encode(frames):
    """frames: (T, H, W) uint8 (H, W multiples of 16). Returns a VID1 container."""
    frames = np.asarray(frames)
    T, H, W = frames.shape
    if H % B or W % B:
        raise ValueError("frame dimensions must be multiples of 16")
    F = frames.astype(np.int64)
    yy, xx = np.meshgrid(np.arange(H), np.arange(W), indexing="ij")
    nby, nbx = H // B, W // B

    out = bytearray(MAGIC)
    out += T.to_bytes(4, "big") + H.to_bytes(4, "big") + W.to_bytes(4, "big")
    # frame 0: all-intra MED
    _put(out, ctxcoder.encode((F[0] - _med_predict(F[0])).reshape(-1)))

    prev = F[0]                                   # == reconstructed previous (lossless)
    for t in range(1, T):
        mode, mvy, mvx, sel, skip_block, inter_block = _choose_modes(
            prev, F[t], yy, xx, nby, nbx)
        _put(out, ctxcoder.encode(mode.reshape(-1)))
        _put(out, ctxcoder.encode(np.concatenate([mvy[inter_block], mvx[inter_block]])))
        _put(out, ctxcoder.encode(_to_blocks(sel, ~skip_block)))
        prev = F[t]
    return bytes(out)


def decode(blob):
    if blob[:4] != MAGIC:
        raise ValueError("not a VID1 stream")
    T = int.from_bytes(blob[4:8], "big")
    H = int.from_bytes(blob[8:12], "big")
    W = int.from_bytes(blob[12:16], "big")
    pos = 16
    yy, xx = np.meshgrid(np.arange(H), np.arange(W), indexing="ij")
    nby, nbx = H // B, W // B

    b0, pos = _take(blob, pos)
    res0 = np.asarray(ctxcoder.decode(b0, H * W), dtype=np.int64).reshape(H, W)
    rec0 = np.empty((H, W), dtype=np.int64)
    _med_fill(rec0, np.ones((H, W), dtype=bool), res0)
    frames = [rec0]
    prev = rec0

    for _ in range(1, T):
        mb, pos = _take(blob, pos)
        mode = np.asarray(ctxcoder.decode(mb, nby * nbx), dtype=np.int64).reshape(nby, nbx)
        skip_block = mode == 2
        intra_block = mode == 1
        inter_block = mode == 0
        n_inter = int(inter_block.sum())
        n_nonskip = int((~skip_block).sum())

        vb, pos = _take(blob, pos)
        mv = np.asarray(ctxcoder.decode(vb, 2 * n_inter), dtype=np.int64)
        rb, pos = _take(blob, pos)
        res = np.asarray(ctxcoder.decode(rb, n_nonskip * B * B), dtype=np.int64)

        mvy = np.zeros((nby, nbx), dtype=np.int64); mvx = np.zeros((nby, nbx), dtype=np.int64)
        mvy[inter_block] = mv[:n_inter]; mvx[inter_block] = mv[n_inter:]
        mc = _predict_qpel(prev, mvy, mvx, yy, xx)
        residual = _from_blocks(res, ~skip_block, H, W)

        skip_px = np.repeat(np.repeat(skip_block, B, 0), B, 1)
        intra_px = np.repeat(np.repeat(intra_block, B, 0), B, 1)
        rec = np.where(skip_px, prev, np.where(intra_px, np.int64(-1 << 30), mc + residual))
        _med_fill(rec, intra_px, residual)
        frames.append(rec)
        prev = rec
    return np.stack(frames).astype(np.uint8)


# --- planar (YUV etc.) -------------------------------------------------------

def encode_yuv(*planes):
    """Encode several independent planes (e.g. Y, U, V) into one container."""
    out = bytearray(MAGIC_YUV)
    out += bytes([len(planes)])
    for p in planes:
        _put8 = encode(p)
        out += len(_put8).to_bytes(8, "big") + _put8
    return bytes(out)


def decode_yuv(blob):
    if blob[:4] != MAGIC_YUV:
        raise ValueError("not a VYUV stream")
    n = blob[4]
    pos = 5
    planes = []
    for _ in range(n):
        ln = int.from_bytes(blob[pos:pos + 8], "big")
        pos += 8
        planes.append(decode(blob[pos:pos + ln]))
        pos += ln
    return planes
