"""Measure-first: FASTQ quality-score stream (Phred bytes) — target #10.

Per-base Phred quality is a low-cardinality stream (here 30 levels; modern NovaSeq bins to ~4)
with strong **position** context (quality drifts along a read) and **prev-value** autocorrelation.
Generic LZ (.fastq.gz = gzip; or zstd/xz) gets some of it; the specialist bar (fqzcomp / SPRING /
Illumina ORA) uses an explicit (position, prev-quality) symbol context model.

Finding: pertype's existing `ctxcoder` is a residual-*magnitude* coder, not a symbol model — it
LOSES here (~-25% vs zstd). But a small adaptive **(prev-q, position-bucket) symbol context model**
on the arithmetic coder beats the generic bar by ~+16%. So the lever is real, but it needs a new
(small) quality codec — unlike the other validated data types, which reused existing codecs.

Bar: gzip/zstd/xz on the raw quality bytes (the .fastq.gz storage form). Ours: the context model
below (encode + decode, round-trip verified; read lengths are cheap side-info, cost included).
Data: a FASTQ (set FASTQ_PATH; .gz ok). Default expects a plain quality file (one read/line) at
FASTQ_QUAL. Download a small real one from ENA:
  curl -O https://ftp.sra.ebi.ac.uk/vol1/fastq/DRR063/DRR063436/DRR063436_1.fastq.gz
  zcat DRR063436_1.fastq.gz | awk 'NR%4==0' > q.txt   # FASTQ_QUAL=q.txt
"""
import gzip
import os
import subprocess
import time

import numpy as np

from pertype.arithmetic import ArithmeticEncoder, ArithmeticDecoder
from pertype.bitio import BitReader

QUAL = os.environ.get("FASTQ_QUAL")
PATH = os.environ.get("FASTQ_PATH")
MAXQ = int(os.environ.get("FASTQ_MAXQ", "3000000"))
INCR, RESCALE = 24, 1 << 14


def read_quality():
    if QUAL:
        reads = [l for l in open(QUAL).read().split("\n") if l]
    else:
        op = gzip.open if PATH.endswith(".gz") else open
        with op(PATH, "rt") as fh:
            reads = [l.rstrip("\n") for i, l in enumerate(fh) if i % 4 == 3]
    vals, pos = [], []
    for r in reads:
        for p, ch in enumerate(r.encode()):
            vals.append(ch); pos.append(p)
        if len(vals) >= MAXQ:
            break
    return np.array(vals, np.int32), np.array(pos, np.int32)


def ctx_of(prevq, p):
    return prevq * 64 + (p if p < 63 else 63)   # (prev-quality, position-bucket)


def encode(S, P, K):
    enc = ArithmeticEncoder(); cnt = {}; tot = {}
    prev = K
    for i in range(S.size):
        c = ctx_of(prev, int(P[i]))
        a = cnt.get(c)
        if a is None:
            a = [1] * K; cnt[c] = a; tot[c] = K
        s = int(S[i]); cum = 0
        for j in range(s):
            cum += a[j]
        enc.encode(cum, a[s], tot[c]); a[s] += INCR; tot[c] += INCR
        if tot[c] >= RESCALE:
            t = 0
            for j in range(K):
                a[j] = (a[j] + 1) >> 1; t += a[j]
            tot[c] = t
        prev = s
    enc.finish()
    return enc.getvalue()


def decode(blob, P, K, n):
    dec = ArithmeticDecoder(BitReader(blob)); cnt = {}; tot = {}
    out = np.empty(n, np.int32); prev = K
    for i in range(n):
        c = ctx_of(prev, int(P[i]))
        a = cnt.get(c)
        if a is None:
            a = [1] * K; cnt[c] = a; tot[c] = K
        target = dec.decode_target(tot[c]); cum = 0; s = 0
        while cum + a[s] <= target:
            cum += a[s]; s += 1
        dec.update(cum, a[s], tot[c]); a[s] += INCR; tot[c] += INCR
        if tot[c] >= RESCALE:
            t = 0
            for j in range(K):
                a[j] = (a[j] + 1) >> 1; t += a[j]
            tot[c] = t
        out[i] = s; prev = s
    return out


def main():
    Q, P = read_quality()
    n = Q.size
    raw = Q.astype(np.uint8).tobytes()
    alpha = sorted(set(Q.tolist())); idx = {v: i for i, v in enumerate(alpha)}; K = len(alpha)
    S = np.array([idx[v] for v in Q.tolist()], np.int32)
    print(f"quality stream: {n/1e6:.2f} MB  {K} Phred levels\n")
    print(f"{'method':<28}{'ratio':>8}{'b/q':>9}")

    def sh(cmd):
        return len(subprocess.run(cmd, input=raw, stdout=subprocess.PIPE).stdout)

    def row(label, size):
        print(f"{label:<28}{n/size:>8.2f}{8*size/n:>9.3f}")

    row("gzip -9 (.fastq.gz form)", sh(["gzip", "-9"]))
    bar = sh(["zstd", "-19", "-c"]); row("zstd -19 (BAR)", bar)
    row("xz -9", sh(["xz", "-9", "-c"]))

    # existing residual coder (the wrong tool — shown for contrast)
    from pertype import ctxcoder
    row("ours ctxcoder (residual)", len(ctxcoder.encode(Q.astype(np.int64).tolist())))

    t = time.time(); blob = encode(S, P, K)
    dec = decode(blob, P, K, n)
    assert np.array_equal(dec, S), "context-model round-trip FAILED"
    side = len(subprocess.run(["zstd", "-19", "-c"],
              input=P.astype(np.uint16).tobytes(), stdout=subprocess.PIPE).stdout)  # read-length side info
    ours = len(blob) + len(alpha) + side
    row("ours (prev-q + position)", ours)
    print(f"\nours vs zstd-19: {(bar-ours)/bar*100:+.1f}%  ({'WIN' if ours < bar else 'lose'} vs generic; "
          f"specialist fqzcomp/SPRING not run — the harder bar)   [{time.time()-t:.0f}s]   round-trip OK")


if __name__ == "__main__":
    main()
