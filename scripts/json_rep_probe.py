"""Probe the json gap to zstd --train: rep-offset hit rates and distance recurrence,
against a cached trained model (so we don't retrain per experiment)."""
import sys

from compressor import codec
from compressor.benchmark import load_split
from compressor.model import Model
from compressor.transform import apply as tapply
from compressor.tokenizer import tokenize_optimal

m = Model.load(open("/tmp/json_model.bin", "rb").read())
_, te = load_split("corpus_real", "json")

# (a) real end-to-end json total today (round-trip verified)
total = 0
for name, d in te:
    c = codec.compress(d, m)
    assert codec.decompress(c, m) == d, name
    total += len(c)
print(f"current json total (round-trip ok): {total:,} B over {len(te)} files", flush=True)

# precompute match distances per test file
dists = []
for _, d in te:
    toks = tokenize_optimal(tapply(d, m.transform), m.dictionary, m.costs(), prefix=m.blob)
    dists.append([t[2] for t in toks if t[0] == "match"])


def simulate(K):
    hits = eb = tot = 0
    for ds in dists:
        reps = list(range(1, K + 1))
        for dist in ds:
            tot += 1
            if dist in reps:
                hits += 1
                i = reps.index(dist)
                reps.pop(i)
                eb += dist.bit_length() - 1
            else:
                reps.pop()
            reps.insert(0, dist)
    return tot, hits, eb


for K in (3, 8, 32):
    tm, h, eb = simulate(K)
    print(f"REP_N={K:>2}: matches={tm:,} hits={h:,} ({100*h/tm:.1f}%) "
          f"dist-extra-bits avoided~{eb/8:,.0f} B", flush=True)

recur = tot = 0
for ds in dists:
    seen = set()
    for dist in ds:
        tot += 1
        if dist in seen:
            recur += 1
        else:
            seen.add(dist)
print(f"within-file distance recurrence: {100*recur/tot:.1f}%", flush=True)
