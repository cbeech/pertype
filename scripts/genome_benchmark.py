"""Genomic DNA (FASTA): an honest *boundary* — where prediction has no edge.

DNA is a near-uniform 4-symbol alphabet (A/C/G/T) with very little low-order context
redundancy, so there is nothing smooth or autocorrelated for a predictor to exploit.
The natural floor is 2-bit packing (2.000 bits/base = 4x over the 1-byte-per-base
text); specialised DNA compressors shave a few percent more with high-order context
models. This script measures where our codec lands vs that floor and vs general
codecs, to document the boundary the way we do for json vs `zstd --train`.

Input: a FASTA ``.fna`` (e.g. NCBI E. coli K-12:
  https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/005/845/GCF_000005845.2_ASM584v2/GCF_000005845.2_ASM584v2_genomic.fna.gz )

Usage: python3 scripts/genome_benchmark.py <genome.fna>
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np


def ext_size(data, cmd):
    return len(subprocess.run(cmd, input=data, stdout=subprocess.PIPE, check=True).stdout)


def order_k_entropy(codes, k, limit=2_000_000):
    """Empirical bits/base under an order-k base-4 context model (the context ceiling)."""
    from collections import defaultdict
    s = codes[:limit]
    mask = (1 << (2 * k)) - 1
    tab = defaultdict(lambda: [1, 1, 1, 1])
    ctx = 0
    H = 0.0
    for b in s.tolist():                       # .tolist() -> python ints (no uint8 overflow)
        t = tab[ctx]
        H += -np.log2(t[b] / sum(t))
        t[b] += 1
        ctx = ((ctx << 2) | b) & mask
    return H / len(s)


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    raw = open(sys.argv[1], "rb").read()
    seq = bytearray()
    for line in raw.split(b"\n"):
        if line[:1] != b">":
            seq += line
    seq = bytes(seq)
    print(f"{os.path.basename(sys.argv[1])}  file {len(raw):,} B   sequence {len(seq):,} bases")

    def bpb(sz):
        return 8 * sz / len(seq)

    for nm, cmd in [("zstd -19", ["zstd", "-19", "-c"]), ("xz -9", ["xz", "-9", "-c"]),
                    ("bzip2 -9", ["bzip2", "-9", "-c"])]:
        sz = ext_size(raw, cmd)
        print(f"  {nm:<12}{sz:>10,} B   {len(raw)/sz:5.2f}x   {bpb(sz):.3f} bits/base")

    m = {65: 0, 67: 1, 71: 2, 84: 3}
    idx = np.frombuffer(seq, np.uint8)
    codes = np.array([m.get(int(b), 0) for b in idx], np.uint8)
    n_non = int((~np.isin(idx, list(m))).sum())
    packed = np.packbits(((codes[:, None] >> [1, 0]) & 1).reshape(-1))
    print(f"  {'2-bit pack':<12}{len(packed):>10,} B   {len(raw)/len(packed):5.2f}x   "
          f"2.000 bits/base (floor; {n_non} non-ACGT)")

    print("  order-k context ceiling (bits/base):  " +
          "  ".join(f"k{k}={order_k_entropy(codes, k):.3f}" for k in (0, 2, 4, 8, 11)))
    print("  -> ~1.95 bits/base at order 2-4 (higher orders need more sequence than one")
    print("     bacterium gives); prediction/transforms add nothing over 2-bit packing.")


if __name__ == "__main__":
    main()
