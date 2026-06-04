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
    pred[0, 0] = 0
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
    pred[0, 0] = 0
    return pred


_PREDICT = {"med": med_predict, "paeth": paeth_predict}


def forward(P, kind):
    """Residual ``P - prediction`` as int32 (signed; feed to ctxcoder)."""
    return P.astype(np.int32) - _PREDICT[kind](P)


def reconstruct(res, kind):
    """Invert ``forward``: causal raster reconstruction. Pure-Python (a native
    port would speed it up, as for the video MED); exact."""
    H, W = res.shape
    rec = np.zeros((H, W), dtype=np.int32)
    med = kind == "med"
    for y in range(H):
        row = rec[y]
        prev = rec[y - 1] if y > 0 else None
        ry = res[y]
        for x in range(W):
            if x == 0:
                pred = prev[0] if y > 0 else 0
            elif y == 0:
                pred = row[x - 1]
            else:
                a = int(row[x - 1]); b = int(prev[x]); c = int(prev[x - 1])
                if med:
                    mx = a if a > b else b
                    mn = a if a < b else b
                    pred = mn if c >= mx else (mx if c <= mn else a + b - c)
                else:
                    p = a + b - c
                    pa = abs(p - a); pb = abs(p - b); pc = abs(p - c)
                    pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
            row[x] = pred + ry[x]
    return rec
