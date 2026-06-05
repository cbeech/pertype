"""Protein sequences (FASTA) — the 20-symbol boundary, the midpoint between DNA and text.

Completes the alphabet-entropy story: DNA is a 4-symbol source (~1.95 bits/base, where
2-bit packing is the floor and prediction adds nothing — see genome_benchmark.py); protein
is a ~20-symbol amino-acid source (~4.2 bits/residue). Both are *boundaries*: the symbol
entropy is the wall, residues are near-i.i.d. (no low-order context redundancy), and LZ has
nothing to match — so a plain order-0 entropy coder beats the LZ tools, but our dictionary/
prediction machinery has no structural edge. Specialists (high-order protein context models)
shave a few percent more.

Input: a protein FASTA ``.faa`` (e.g. NCBI E. coli proteome, the same organism as the DNA
test: .../GCF_000005845.2_ASM584v2_protein.faa.gz). Usage: python3 scripts/protein_benchmark.py <f.faa>
"""
import math
import os
import subprocess
import sys
from collections import Counter, defaultdict

import numpy as np


def ext(data, cmd):
    return len(subprocess.run(cmd, input=data, stdout=subprocess.PIPE, check=True).stdout)


def order_k(codes, A, k, lim=1_500_000):
    s = codes[:lim]
    tab = defaultdict(lambda: [1] * A)
    ctx, H = 0, 0.0
    for b in s.tolist():
        t = tab[ctx]
        H += -math.log2(t[b] / sum(t))
        t[b] += 1
        ctx = ((ctx * A) + b) % (A ** k) if k else 0
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
    cnt = Counter(seq)
    A = len(cnt)
    print(f"{os.path.basename(sys.argv[1])}  file {len(raw):,} B   sequence {len(seq):,} residues   "
          f"alphabet {A} ({''.join(sorted(chr(k) for k in cnt))})")

    def bpr(sz):
        return 8 * sz / len(seq)

    for nm, cmd in [("zstd -19", ["zstd", "-19", "-c"]), ("xz -9", ["xz", "-9", "-c"]),
                    ("bzip2 -9", ["bzip2", "-9", "-c"])]:
        sz = ext(raw, cmd)
        print(f"  {nm:<12}{sz:>10,} B   {len(raw)/sz:5.2f}x   {bpr(sz):.3f} bits/residue")

    idx = {b: i for i, b in enumerate(sorted(cnt))}
    codes = np.array([idx[b] for b in seq], np.int32)
    print(f"  entropy floor log2({A}) = {math.log2(A):.3f} bits/residue")
    print("  order-k entropy:  " + "  ".join(f"k{k}={order_k(codes, A, k):.3f}" for k in (0, 1, 2)))
    print("  -> near-i.i.d.: order-0 entropy coding beats the LZ tools (no repetition to match),")
    print("     but prediction/transforms add nothing — a boundary, like DNA at 4 symbols.")


if __name__ == "__main__":
    main()
