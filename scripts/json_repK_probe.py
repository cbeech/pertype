"""Net cost of a larger repeat-offset cache on json, priced end-to-end.

For each candidate REP_N=K we rebuild the mode+dist frequency models from the
training tokens (simulated under a K-entry move-to-front cache) and price the
held-out match-coding cost (dist_sym + dist_extra + mode). main_model / dict /
len are unchanged by K, so we hold them fixed and report only the deltas plus the
full implied json total."""
import math
from collections import Counter

from compressor.benchmark import load_split
from compressor.model import Model, main_alphabet_base
from compressor.transform import apply as tapply
from compressor.tokenizer import (
    tokenize_optimal, value_slot, MIN_MATCH, MAX_DIST_SLOT,
)
from compressor.freqmodel import FrequencyModel

m = Model.load(open("/tmp/json_model.bin", "rb").read())
tr, te = load_split("corpus_real", "json")
lb = m.len_base


def toks(d):
    return tokenize_optimal(tapply(d, m.transform), m.dictionary, m.costs(), prefix=m.blob)


train_tok = [toks(d) for _, d in tr]
test_tok = [toks(d) for _, d in te]


def build_and_price(K):
    # rebuild mode + dist counts from training tokens under a K-cache
    mode_c = Counter({i: 1 for i in range(K + 1)})
    dist_c = Counter({s: 1 for s in range(MAX_DIST_SLOT + 1)})
    for tokens in train_tok:
        reps = list(range(1, K + 1))
        for t in tokens:
            if t[0] != "match":
                continue
            dist = t[2]
            if dist in reps:
                i = reps.index(dist)
                mode_c[i + 1] += 1
                reps.pop(i)
            else:
                mode_c[0] += 1
                ds, _ = value_slot(dist)
                dist_c[ds] += 1
                reps.pop()
            reps.insert(0, dist)
    mode_m = FrequencyModel.from_counts(mode_c)
    dist_m = FrequencyModel.from_counts(dist_c)
    # price held-out match-coding cost
    bits = 0.0
    for tokens in test_tok:
        reps = list(range(1, K + 1))
        for t in tokens:
            if t[0] != "match":
                continue
            dist = t[2]
            if dist in reps:
                i = reps.index(dist)
                bits += mode_m.cost_bits(i + 1)
                reps.pop(i)
            else:
                bits += mode_m.cost_bits(0)
                ds, extra = value_slot(dist)
                bits += dist_m.cost_bits(ds) + ds
                reps.pop()
            reps.insert(0, dist)
    return bits / 8


base = build_and_price(3)
print(f"K= 3 match-coding cost: {base:,.0f} B  (current; json total 54,524)", flush=True)
for K in (4, 6, 8, 12, 16, 32):
    c = build_and_price(K)
    print(f"K={K:>2} match-coding cost: {c:,.0f} B  (delta {c-base:+,.0f} B -> json ~{54524+(c-base):,.0f})",
          flush=True)
