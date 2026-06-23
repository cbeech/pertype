"""Measure-first: mocap / animation curves — BVH joint-channel matrices (real CMU database) — Tier-2.

Premise: a BVH file stores a frames × channels matrix of per-joint Euler angles (+ root position)
that vary SMOOTHLY frame-to-frame, but the field ships it as verbose TEXT and compresses with
generic gzip — which sees the matrix row-major (interleaving all ~96 channels per frame) and
cannot exploit each channel's temporal continuity. The lever = transpose to channel-major +
temporal prediction + entropy code (the same per-channel predictor + ctxcoder behind the EEG win).

VERDICT (real data: 10 CMU BVH clips, 6 subjects, 96 channels @120 Hz; values are exact fixed-point
×1e4 so the int matrix is lossless of the content; per-channel round-trip verified):
- ✅ **VALIDATED.** pertype (chan-major transpose + order-1/2 temporal predictor + `ctxcoder`) =
  **5.90× total vs the original text bytes — +41% vs text-xz (3.50×) and +112% vs the as-shipped
  text-gzip (2.78×)**. Against a *sophisticated* generic pipeline (the obvious transpose+Δ+`xz -9`
  trick, 5.22×) pertype still wins **+11.6%** — that margin is the genuine pertype edge: its
  context-adaptive arithmetic coder beats xz on the per-channel residual, plus the order-2
  predictor (best on 9/10 clips). Consistent per-file: +38–43% vs text-xz, +10.6–13.5% vs Δ+xz.
- Why it works (and NetFlow didn't): a mocap matrix is dominated by **smooth, temporally-correlated**
  channels where prediction collapses the residual — the predictor's home turf — unlike a flow
  record dominated by non-sequential high-card fields. The row-major text + generic LZ is exactly
  the "smooth numeric matrix stored as text, no per-channel temporal model" gap the codec closes
  (same shape as thermal-IR's temporal lever and the multispectral/depth 2D-predictor wins).
- Scope: lossless of the fixed-point joint values (the meaningful content; CMU BVH is uniformly
  4-decimal, so value-exact ≈ byte-exact). Lossy mocap compressors (PCA/spline curve-fitting) exist
  but are out of scope for this lossless comparison; there is no deployed lossless mocap specialist,
  so the strongest real bar is the generic transpose+Δ+xz pipeline, which we beat by +11.6%.

Data: a representative sample of the public CMU Graphics Lab Motion Capture Database in BVH form
(una-dinosauria/cmu-mocap mirror), fetched over plain HTTPS (no login).
"""
import glob, gzip, lzma, os, subprocess, sys, urllib.request
import numpy as np
from pertype import ctxcoder

RAW = "https://raw.githubusercontent.com/una-dinosauria/cmu-mocap/master/data"
SAMPLE = ["001/01_01", "001/01_02", "001/01_03", "002/02_01", "002/02_02",
          "005/05_01", "007/07_01", "008/08_01", "016/16_01", "035/35_01"]
DDIR = sys.argv[1] if len(sys.argv) > 1 else "bvh"


def fetch():
    os.makedirs(DDIR, exist_ok=True)
    for p in SAMPLE:
        f = os.path.join(DDIR, p.replace("/", "_") + ".bvh")
        if not os.path.exists(f):
            urllib.request.urlretrieve(f"{RAW}/{p}.bvh", f)
    return sorted(glob.glob(os.path.join(DDIR, "*.bvh")))


def zstd(b, l=19):
    return len(subprocess.run(["zstd", f"-{l}", "-c"], input=b, stdout=subprocess.PIPE).stdout)


def xz(b):
    return len(lzma.compress(b, preset=9))


def load(fn):
    txt = open(fn).read().splitlines()
    mi = [i for i, l in enumerate(txt) if l.strip() == "MOTION"][0]
    nf = int(txt[mi + 1].split(":")[1])
    rows = [r.split() for r in txt[mi + 3:mi + 3 + nf] if r.strip()]
    M = np.array([[float(x) for x in r] for r in rows])
    Q = np.round(M * 1e4).astype(np.int64)
    assert np.array_equal(Q / 1e4, M), f"not fixed-point x1e4: {fn}"   # lossless of values
    return ("\n".join(txt)).encode(), Q


def pertype_code(Q, order):
    """chan-major transpose + order-1/2 temporal predictor -> ctxcoder; returns (bytes, round-trip ok)."""
    T = Q.T
    R = T.copy(); R[:, 1:] = T[:, 1:] - T[:, :-1]
    if order == 2:
        R2 = R.copy(); R2[:, 1:] = R[:, 1:] - R[:, :-1]; R = R2
    res = R.reshape(-1).tolist()
    blob = ctxcoder.encode(res)
    back = np.array(ctxcoder.decode(blob, len(res)), np.int64).reshape(T.shape)
    rec = np.cumsum(back, axis=1)
    if order == 2:
        rec = np.cumsum(rec, axis=1)
    return len(blob), bool((rec == T).all())


def main():
    files = fetch()
    tot = {k: 0 for k in ("text", "tgz", "tzs", "txz", "bxz", "dxz", "pt")}
    for fn in files:
        text, Q = load(fn)
        rowmaj = np.ascontiguousarray(Q.astype("<i4")).tobytes()
        D = Q.T.copy(); D[:, 1:] = Q.T[:, 1:] - Q.T[:, :-1]
        dxz = xz(np.ascontiguousarray(D.astype("<i4")).tobytes())
        b1, ok1 = pertype_code(Q, 1)
        b2, ok2 = pertype_code(Q, 2)
        assert ok1 and ok2, f"round-trip failed: {fn}"
        pt = min(b1, b2)
        r = {"text": len(text), "tgz": len(gzip.compress(text, 9)), "tzs": zstd(text),
             "txz": xz(text), "bxz": xz(rowmaj), "dxz": dxz, "pt": pt}
        for k in tot: tot[k] += r[k]
        print(f"{os.path.basename(fn):16} text {r['text']/1e3:6.0f}KB | "
              f"txz {r['text']/r['txz']:4.2f}x  Δ+xz {r['text']/r['dxz']:4.2f}x  "
              f"pertype(o{1 if b1 < b2 else 2}) {r['text']/pt:4.2f}x  "
              f"[vs txz {(1-pt/r['txz'])*100:+5.1f}%, vs Δ+xz {(1-pt/r['dxz'])*100:+5.1f}%]",
              flush=True)
    print(f"\n== TOTAL ({len(files)} files), ratio vs original text bytes ==")
    for k, lbl in [("tgz", "text+gzip (as shipped)"), ("tzs", "text+zstd"), ("txz", "text+xz"),
                   ("bxz", "int32+xz"), ("dxz", "transpose+Δ+xz (generic best)"),
                   ("pt", "pertype chan-predict+ctxcoder")]:
        print(f"   {lbl:32} {tot['text']/tot[k]:5.2f}x   vs generic-best {(1-tot[k]/tot['dxz'])*100:+6.1f}%")


if __name__ == "__main__":
    main()
