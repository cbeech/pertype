"""Is there recoverable structure in the distance 'extra' bits (coded raw today)?

For normal-mode matches (after a depth-16 rep cache), each distance is coded as a
slot symbol + `slot` raw uniform bits. We test whether the top-k of those extra
bits carry entropy: train a per-(slot, prefix) model on the training set, price the
held-out test set, and compare to coding them raw."""
import math
from collections import Counter, defaultdict

from compressor.benchmark import load_split
from compressor.model import Model, REP_N
from compressor.transform import apply as tapply
from compressor.tokenizer import tokenize_optimal, value_slot

m = Model.load(open("/tmp/json_model.bin", "rb").read())
tr, te = load_split("corpus_real", "json")


def normal_dists(tokens):
    """Yield distances that are coded in normal mode under a REP_N move-to-front cache."""
    reps = list(range(1, REP_N + 1))
    for t in tokens:
        if t[0] != "match":
            continue
        d = t[2]
        if d in reps:
            i = reps.index(d)
            reps.pop(i)
        else:
            reps.pop()
            yield d
        reps.insert(0, d)


def toks(d):
    return tokenize_optimal(tapply(d, m.transform), m.dictionary, m.costs(),
                            prefix=m.blob, max_chain=512)


train_d = [d for _, doc in tr for d in normal_dists(toks(doc))]
test_d = [d for _, doc in te for d in normal_dists(toks(doc))]
print(f"normal distances: train={len(train_d):,} test={len(test_d):,}", flush=True)

# baseline raw extra-bit cost on test
raw_extra = sum(value_slot(d)[0] for d in test_d)
print(f"baseline raw extra bits: {raw_extra/8:,.0f} B", flush=True)

for K in (1, 2, 3, 4, 6, 8):
    # train: per (slot, bit_position, prefix) -> P(next bit)
    # model the top K extra bits as a binary tree conditioned on slot+prefix
    ctx = defaultdict(lambda: [1, 1])  # (slot, prefix) -> [count0, count1] for next bit
    for d in train_d:
        s, e = value_slot(d)
        k = min(K, s)
        prefix = 1  # leading sentinel so prefixes of different length differ
        for b in range(k):
            bit = (e >> (s - 1 - b)) & 1
            ctx[(s, prefix)][bit] += 1
            prefix = (prefix << 1) | bit
    # price test
    coded = 0.0
    for d in test_d:
        s, e = value_slot(d)
        k = min(K, s)
        prefix = 1
        for b in range(k):
            bit = (e >> (s - 1 - b)) & 1
            c = ctx[(s, prefix)]
            p = c[bit] / (c[0] + c[1])
            coded += -math.log2(p)
            prefix = (prefix << 1) | bit
        coded += (s - k)  # remaining bits raw
    print(f"K={K}: modeled-extra cost {coded/8:,.0f} B  (saves {(raw_extra-coded)/8:+,.0f} B vs raw)",
          flush=True)
