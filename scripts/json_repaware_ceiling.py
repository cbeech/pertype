"""Ceiling estimate for a repeat-offset-aware parser on json.

Keeps our cost-optimal parse (so parse quality is held fixed) and only re-prices
it: for each normal-mode match, if an equally-long match also exists at one of the
current repeat-offset distances, code it as a rep hit instead of a full distance.
This is a strict *lower bound* on a rep-aware parser's benefit (a real one could
also change match lengths/positions), so if even this clears the gap, the rewrite
is worth it; if it doesn't, the rewrite can't win either."""
import math
from collections import Counter

from compressor.benchmark import load_split
from compressor.model import Model, main_alphabet_base, REP_N, MODE_NORMAL
from compressor.transform import apply as tapply
from compressor.tokenizer import tokenize_optimal, value_slot, MIN_MATCH

m = Model.load(open("/tmp/json_model.bin", "rb").read())
_, te = load_split("corpus_real", "json")
lb = m.len_base
main, dist, mode = m.main_model, m.dist_model, m.mode_model

# We need rebuilt mode/dist models that reflect rep-swapping, but for a ceiling we
# just price with the existing trained models (cost_bits). Good enough to compare.


def matches_at(buf, p, r, L):
    """True if a length-L match exists at distance r ending forward from p."""
    if r > p:
        return False
    src = p - r
    # compare L bytes (overlapping copy semantics: src may be within [p-r, p))
    for k in range(L):
        if buf[p + k] != buf[src + k]:
            return False
    return True


base_bits = 0.0
swap_bits = 0.0
nmatch = swaps = 0
for _, d in te:
    td = tapply(d, m.transform)
    toks = tokenize_optimal(td, m.dictionary, m.costs(), prefix=m.blob, max_chain=512)
    buf = bytearray(m.blob) + bytearray(td)
    pos = len(m.blob)
    reps_b = list(range(1, REP_N + 1))   # baseline rep cache (no swapping)
    reps_s = list(range(1, REP_N + 1))   # rep-aware cache (with swapping)
    for t in toks:
        if t[0] == "lit":
            base_bits += main.cost_bits(t[1]); swap_bits += main.cost_bits(t[1])
            pos += 1
        elif t[0] == "dict":
            base_bits += main.cost_bits(256 + t[1]); swap_bits += main.cost_bits(256 + t[1])
            pos += len(m.dictionary.patterns[t[1]])
        else:
            length, distance = t[1], t[2]
            nmatch += 1
            lslot, _ = value_slot(length - MIN_MATCH + 1)
            lc = main.cost_bits(lb + lslot) + lslot
            base_bits += lc; swap_bits += lc

            # --- baseline pricing (rep-unaware parse, existing rep cache) ---
            if distance in reps_b:
                i = reps_b.index(distance); base_bits += mode.cost_bits(i + 1); reps_b.pop(i)
            else:
                base_bits += mode.cost_bits(MODE_NORMAL)
                dslot, _ = value_slot(distance)
                base_bits += dist.cost_bits(dslot) + dslot
                reps_b.pop()
            reps_b.insert(0, distance)

            # --- rep-aware pricing: prefer a cached distance with an equal-length match ---
            chosen = distance
            if distance in reps_s:
                i = reps_s.index(distance); swap_bits += mode.cost_bits(i + 1); reps_s.pop(i)
            else:
                # look for a rep distance that yields a length-`length` match here
                hit = -1
                for idx, r in enumerate(reps_s):
                    if matches_at(buf, pos, r, length):
                        hit = idx; break
                if hit >= 0:
                    swaps += 1
                    r = reps_s[hit]
                    swap_bits += mode.cost_bits(hit + 1)
                    reps_s.pop(hit)
                    chosen = r  # MTF the rep distance actually used for the copy
                else:
                    swap_bits += mode.cost_bits(MODE_NORMAL)
                    dslot, _ = value_slot(distance)
                    swap_bits += dist.cost_bits(dslot) + dslot
                    reps_s.pop()
            reps_s.insert(0, chosen)
            pos += length

hdr = 12 * len(te)  # ~ current varint header per file
print(f"matches={nmatch:,}  rep-swaps found={swaps:,} ({100*swaps/nmatch:.1f}%)")
print(f"baseline payload (rep-unaware parse): {base_bits/8 + hdr:,.0f} B (+~{hdr} hdr)")
print(f"rep-aware ceiling:                    {swap_bits/8 + hdr:,.0f} B")
print(f"ceiling saving: {(base_bits-swap_bits)/8:,.0f} B   vs zstd 49,741")
