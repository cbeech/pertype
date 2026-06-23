"""Measure-first: scRNA-seq sparse count matrices (10x MTX) — Tier-2 lead.

A single-cell count matrix is a huge sparse integer matrix (UMI counts, mostly 1-3). Stored as
CSC/CSR arrays (data + indices + indptr); the incumbent is gzip/blosc per array. The count VALUES
are skewed small ints (the sparse-data regime our ctxcoder wins on); the row-INDEX array (sorted
gene ids per cell → delta) is already near-optimal for generic LZ.

Result (real 10x pbmc3k, 2.29M nonzeros): ours beats generic on the COUNTS (+12%, 14.2× vs zstd
12.4×) but ties on the indices, so the TOTAL is only **+3%** — marginal. The counts win confirms
the sparse-int lesson, but the index arrays dominate and don't improve. Verdict: ⚠️ marginal.

Bar: gzip/zstd per array. Ours: ctxcoder on counts; delta-within-column + ctxcoder on row indices.
Data: a 10x MatrixMarket file. Default SCRNA_MTX; download pbmc3k:
  curl -O https://cf.10xgenomics.com/samples/cell-exp/1.1.0/pbmc3k/pbmc3k_filtered_gene_bc_matrices.tar.gz
"""
import os
import subprocess

import numpy as np

from pertype import ctxcoder

MTX = os.environ.get("SCRNA_MTX", "data/scrna/matrix.mtx")


def sh(data, cmd):
    return len(subprocess.run(cmd, input=data, stdout=subprocess.PIPE).stdout)


def main():
    with open(MTX) as f:
        for ln in f:
            if not ln.startswith("%"):
                dims = ln.strip(); break
        arr = np.loadtxt(f, dtype=np.int64)
    r, c, v = arr[:, 0], arr[:, 1], arr[:, 2]
    nnz = len(v)
    order = np.lexsort((r, c)); r, c, v = r[order], c[order], v[order]
    dr = r.copy(); dr[1:] = r[1:] - r[:-1]
    cs = np.zeros(nnz, bool); cs[0] = True; cs[1:] = c[1:] != c[:-1]; dr[cs] = r[cs]
    print(f"scRNA matrix {dims}  nnz={nnz:,}  counts 1-{int(v.max())} ({len(np.unique(v))} distinct)\n")
    tot_n = tot_o = tot_b = 0
    for name, vals, ci in [("counts (data)", v, v), ("row indices (Δ/col)", r, dr)]:
        raw = vals.astype("<i4").tobytes(); N = len(raw)
        best = min(sh(raw, ["gzip", "-9"]), sh(raw, ["zstd", "-19", "-c"]))
        blob = ctxcoder.encode(ci.tolist())
        assert np.array_equal(np.asarray(ctxcoder.decode(blob, nnz)), ci), f"RT {name}"
        print(f"  {name:22} ours {N/len(blob):5.2f}x  vs best-generic {N/best:5.2f}x  "
              f"({(best-len(blob))/best*100:+.0f}%)")
        tot_n += N; tot_o += len(blob); tot_b += best
    print(f"\n  TOTAL: ours {tot_n/tot_o:.2f}x  vs best-generic {tot_n/tot_b:.2f}x  "
          f"-> {(tot_b-tot_o)/tot_b*100:+.0f}%  (counts win, indices tie → marginal)   round-trip OK")


if __name__ == "__main__":
    main()
