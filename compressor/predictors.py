"""Shared causal 2D intra predictors for image and video paths.

* **MED** — the JPEG-LS / LOCO-I median edge detector: predict a pixel from its
  left (W), up (N), and up-left (NW) neighbours, picking min/max/gradient by where
  NW sits relative to W and N. Strong on natural images (it follows edges).
* **Paeth** — PNG's predictor: of W, N, NW, pick the one closest to W+N-NW.

The forward pass is vectorised yet **exact for lossless reconstruction**: every
neighbour a pixel predicts from is causal (left/up/up-left), and those are
reconstructed byte-identically, so the prediction during decode equals the one
during encode. ``reconstruct`` replays the predictor causally to invert.

The same MED lives in the video intra path (videocodec); keeping it here lets both
share one definition.
"""
import numpy as np

_native = None


def _get_native():
    global _native
    if _native is None:
        try:
            from compressor import native as n
            _native = n if n.HAVE_NATIVE else False
        except Exception:
            _native = False
    return _native


# Origin (top-left) prediction: 128, matching the native ``med_fill`` and the video
# intra path, so the vectorised forward here and that fast C reconstruction agree.
_ORIGIN = 128


def _neighbours(P):
    a = np.zeros_like(P); a[:, 1:] = P[:, :-1]      # W  (left)
    b = np.zeros_like(P); b[1:, :] = P[:-1, :]      # N  (up)
    c = np.zeros_like(P); c[1:, 1:] = P[:-1, :-1]   # NW (up-left)
    return a, b, c


def med_predict(P):
    """JPEG-LS median predictor over an int32 plane ``P`` (full-array form)."""
    P = P.astype(np.int32)
    a, b, c = _neighbours(P)
    mx, mn = np.maximum(a, b), np.minimum(a, b)
    pred = np.where(c >= mx, mn, np.where(c <= mn, mx, a + b - c))
    pred[0, 1:] = P[0, :-1]      # first row: predict from the left
    pred[1:, 0] = P[:-1, 0]      # first col: predict from above
    pred[0, 0] = _ORIGIN
    return pred


def paeth_predict(P):
    """PNG Paeth predictor over an int32 plane ``P`` (full-array form)."""
    P = P.astype(np.int32)
    a, b, c = _neighbours(P)
    p = a + b - c
    pa, pb, pc = np.abs(p - a), np.abs(p - b), np.abs(p - c)
    pred = np.where((pa <= pb) & (pa <= pc), a, np.where(pb <= pc, b, c))
    pred[0, 1:] = P[0, :-1]
    pred[1:, 0] = P[:-1, 0]
    pred[0, 0] = _ORIGIN
    return pred


def gap_predict(P, scale=1):
    """CALIC gradient-adjusted predictor over an int32 plane. Uses 2-back
    neighbours (W,N,NE,NW,WW,NN) and a gradient test to follow edges; thresholds
    ``80/32/8 * scale`` adapt to bit depth. All divisions are arithmetic shifts so
    this is byte-identical to the native ``gap_fill`` reconstruction."""
    P = P.astype(np.int32)
    a, b, nw = _neighbours(P)                        # W, N, NW
    ne = np.zeros_like(P); ne[1:, :-1] = P[:-1, 1:]  # NE (up-right)
    ww = np.zeros_like(P); ww[:, 2:] = P[:, :-2]     # WW (left-left)
    nn = np.zeros_like(P); nn[2:, :] = P[:-2, :]     # NN (up-up)
    dh = np.abs(a - ww) + np.abs(b - nw) + np.abs(b - ne)
    dv = np.abs(a - nw) + np.abs(b - nn) + np.abs(ne - nn)
    base = ((a + b) >> 1) + ((ne - nw) >> 2)
    d = dv - dh
    t1, t2, t3 = 80 * scale, 32 * scale, 8 * scale
    pred = np.where(d > t1, a, np.where(d < -t1, b,
           np.where(d > t2, (base + a) >> 1, np.where(d < -t2, (base + b) >> 1,
           np.where(d > t3, (3 * base + a) >> 2, np.where(d < -t3, (3 * base + b) >> 2,
           base))))))
    pred[0, 1:] = P[0, :-1]
    pred[1:, 0] = P[:-1, 0]
    pred[0, 0] = _ORIGIN
    return pred


_CALIC_TH = (1, 3, 6, 11, 18, 30, 50, 90, 160, 300)


def _calic_py(data, scale, encode):
    """Pure-Python CALIC bias correction, byte-identical to the native ``calic_code``.
    ``encode``: data=image -> residual; else data=residual -> image."""
    H, W = data.shape
    out = np.zeros((H, W), dtype=np.int64)
    img, res = (data, out) if encode else (out, data)
    th = [t * scale for t in _CALIC_TH]
    t1, t2, t3 = 80 * scale, 32 * scale, 8 * scale
    B = [0] * 704
    C = [0] * 704
    for y in range(H):
        e_left = 0
        for x in range(W):
            a = int(img[y, x - 1]) if x > 0 else 0
            b = int(img[y - 1, x]) if y > 0 else 0
            nw = int(img[y - 1, x - 1]) if (x > 0 and y > 0) else 0
            ne = int(img[y - 1, x + 1]) if (y > 0 and x < W - 1) else 0
            ww = int(img[y, x - 2]) if x > 1 else 0
            nn = int(img[y - 2, x]) if y > 1 else 0
            if y == 0 and x == 0:
                pred = 128
            elif y == 0:
                pred = a
            elif x == 0:
                pred = b
            else:
                base = ((a + b) >> 1) + ((ne - nw) >> 2)
                d = (abs(a - nw) + abs(b - nn) + abs(ne - nn)) \
                    - (abs(a - ww) + abs(b - nw) + abs(b - ne))
                if d > t1: pred = a
                elif d < -t1: pred = b
                elif d > t2: pred = (base + a) >> 1
                elif d < -t2: pred = (base + b) >> 1
                elif d > t3: pred = (3 * base + a) >> 2
                elif d < -t3: pred = (3 * base + b) >> 2
                else: pred = base
            dh = abs(a - ww) + abs(b - nw) + abs(b - ne)
            dv = abs(a - nw) + abs(b - nn) + abs(ne - nn)
            energy = dh + dv + 2 * abs(e_left)
            delta = 0
            while delta < 10 and energy >= th[delta]:
                delta += 1
            tex = ((a >= pred) | ((b >= pred) << 1) | ((nw >= pred) << 2)
                   | ((ne >= pred) << 3) | ((ww >= pred) << 4) | ((nn >= pred) << 5))
            k = delta * 64 + tex
            ck = C[k]
            if ck <= 0:
                corr = 0
            elif B[k] >= 0:
                corr = (B[k] + ck // 2) // ck
            else:
                corr = -(((-B[k]) + ck // 2) // ck)
            if encode:
                e = int(img[y, x]) - pred
                res[y, x] = e - corr
            else:
                e = int(res[y, x]) + corr
                img[y, x] = e + pred
            B[k] += e
            C[k] += 1
            if C[k] >= 256:
                B[k] >>= 1
                C[k] >>= 1
            e_left = e
    return (res if encode else img).astype(np.int32)


_PREDICT = {"med": med_predict, "paeth": paeth_predict}


def forward(P, kind, scale=1):
    """Residual to feed ctxcoder. ``med``/``paeth``/``gap`` give ``P - prediction``;
    ``calic`` adds CALIC context bias correction (sequential, native when available)."""
    if kind == "calic":
        nat = _get_native()
        if nat:
            return nat.calic_encode(P.astype(np.int32), scale)
        return _calic_py(np.ascontiguousarray(P, dtype=np.int64), scale, encode=True)
    pred = gap_predict(P, scale) if kind == "gap" else _PREDICT[kind](P)
    return P.astype(np.int32) - pred


def reconstruct(res, kind, scale=1):
    """Invert ``forward``: causal raster reconstruction. Uses the native ``med_fill``
    / ``gap_fill`` / ``calic_code`` (byte-identical to the loops below) when
    available, else the pure-Python raster. Exact."""
    res = np.ascontiguousarray(res)
    H, W = res.shape
    if kind == "calic":
        nat = _get_native()
        if nat:
            return nat.calic_decode(res, scale)
        return _calic_py(np.ascontiguousarray(res, dtype=np.int64), scale, encode=False)
    nat = _get_native()
    if nat and kind in ("med", "gap"):
        rec = np.zeros((H, W), dtype=np.int64)
        intra = np.ones((H, W), dtype=np.uint8)
        r64 = np.ascontiguousarray(res, dtype=np.int64)
        if kind == "med":
            nat.med_fill(rec, intra, r64)
        else:
            nat.gap_fill(rec, intra, r64, 80 * scale, 32 * scale, 8 * scale)
        return rec.astype(np.int32)

    rec = np.zeros((H, W), dtype=np.int32)
    t1, t2, t3 = 80 * scale, 32 * scale, 8 * scale
    for y in range(H):
        row = rec[y]
        prev = rec[y - 1] if y > 0 else None
        prev2 = rec[y - 2] if y > 1 else None
        ry = res[y]
        for x in range(W):
            if x == 0:
                pred = prev[0] if y > 0 else _ORIGIN
            elif y == 0:
                pred = row[x - 1]
            elif kind == "med":
                a = int(row[x - 1]); b = int(prev[x]); c = int(prev[x - 1])
                mx = a if a > b else b
                mn = a if a < b else b
                pred = mn if c >= mx else (mx if c <= mn else a + b - c)
            elif kind == "paeth":
                a = int(row[x - 1]); b = int(prev[x]); c = int(prev[x - 1])
                p = a + b - c
                pa = abs(p - a); pb = abs(p - b); pc = abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            else:  # gap
                a = int(row[x - 1]); b = int(prev[x]); nw = int(prev[x - 1])
                ne = int(prev[x + 1]) if x < W - 1 else 0
                ww = int(row[x - 2]) if x > 1 else 0
                nn = int(prev2[x]) if y > 1 else 0
                dh = abs(a - ww) + abs(b - nw) + abs(b - ne)
                dv = abs(a - nw) + abs(b - nn) + abs(ne - nn)
                base = ((a + b) >> 1) + ((ne - nw) >> 2)
                d = dv - dh
                if d > t1: pred = a
                elif d < -t1: pred = b
                elif d > t2: pred = (base + a) >> 1
                elif d < -t2: pred = (base + b) >> 1
                elif d > t3: pred = (3 * base + a) >> 2
                elif d < -t3: pred = (3 * base + b) >> 2
                else: pred = base
            row[x] = pred + ry[x]
    return rec
