"""Reversible tokenization: data <-> a list of semantic tokens.

Tokens are tuples, one of:
  * ``("lit", byte)``              a single literal byte
  * ``("dict", pattern_id)``       a reference to a trained dictionary pattern
  * ``("match", length, distance)`` an LZ77 back-reference into THIS file's own
                                     already-emitted output

The dictionary captures *cross-file* structure (common to the type, referencable
even on a string's first appearance). LZ matches capture *in-file* repetition
(repeated log lines, table rows) that the dictionary can't. Together they cover
both, which is what makes the codec competitive with zstd's trained dictionary.

``detokenize(tokenize(data, d), d) == data`` always holds.
"""

WINDOW = 1 << 16          # how far back an LZ match may reach
MIN_MATCH = 3             # shortest worthwhile match (dict or LZ)
MAX_MATCH = 1 << 12       # cap on a single match length
MAX_CHAIN = 64            # hash-chain search depth (speed vs. ratio)

# A short LZ match only pays off when it's nearby: the distance code costs ~slot
# bits, so a 3-byte match 5 KB away can cost more than just emitting 3 cheap
# literals. Accept short matches only within these distance caps; longer matches
# are always worth it.
_SHORT_MATCH_DISTANCE_CAP = {3: 1 << 7, 4: 1 << 11, 5: 1 << 14}


# A dictionary reference is a single cheap symbol with no distance code, so an
# LZ match must be at least this many bytes longer than an available dict match
# before it's worth paying the LZ distance cost to use it instead.
_LZ_OVER_DICT_MARGIN = 4


def _accept_lz(length, distance):
    if length < MIN_MATCH:
        return False
    cap = _SHORT_MATCH_DISTANCE_CAP.get(length)
    return cap is None or distance <= cap


def value_slot(v):
    """Bucket an integer ``v >= 1`` into (slot, extra_value).

    ``slot`` indexes a power-of-two range and also equals the number of extra
    bits that carry ``extra_value``. Reconstruct with :func:`value_from`.
    """
    slot = v.bit_length() - 1
    return slot, v - (1 << slot)


def value_from(slot, extra_value):
    return (1 << slot) + extra_value


# Largest slots that can ever be emitted, used to size the entropy alphabets.
MAX_LEN_SLOT = value_slot(MAX_MATCH - MIN_MATCH + 1)[0]
MAX_DIST_SLOT = value_slot(WINDOW)[0]


def tokenize(data, dictionary, use_lz=True, window=WINDOW, min_match=MIN_MATCH,
             max_match=MAX_MATCH, max_chain=MAX_CHAIN):
    n = len(data)

    if not use_lz:
        # Dictionary + literals only — no in-file back-references.
        tokens = []
        pos = 0
        while pos < n:
            dm = dictionary.match(data, pos, min_match)
            if dm is not None and dm[1] >= min_match:
                tokens.append(("dict", dm[0]))
                pos += dm[1]
            else:
                tokens.append(("lit", data[pos]))
                pos += 1
        return tokens

    tokens = []
    head = {}                 # 3-byte key -> most recent position
    prev = [-1] * n           # position -> previous position with same 3 bytes

    def insert(i):
        if i + min_match <= n:
            key = data[i : i + min_match]
            prev[i] = head.get(key, -1)
            head[key] = i

    pos = 0
    while pos < n:
        # Best in-file LZ match via hash chains.
        best_len, best_dist = 0, 0
        if pos + min_match <= n:
            cand = head.get(data[pos : pos + min_match], -1)
            chain = max_chain
            limit = min(max_match, n - pos)
            while cand != -1 and pos - cand <= window and chain > 0:
                length = 0
                while length < limit and data[cand + length] == data[pos + length]:
                    length += 1
                if length > best_len:
                    best_len, best_dist = length, pos - cand
                    if length == limit:
                        break
                cand = prev[cand]
                chain -= 1

        # Best trained-dictionary match.
        dm = dictionary.match(data, pos, min_match)
        dict_len = dm[1] if dm else 0

        use_dict = dict_len >= min_match
        lz_ok = _accept_lz(best_len, best_dist)

        if use_dict and (not lz_ok or best_len < dict_len + _LZ_OVER_DICT_MARGIN):
            tokens.append(("dict", dm[0]))   # cheapest: one symbol, no distance
            advance = dict_len
        elif lz_ok:
            tokens.append(("match", best_len, best_dist))
            advance = best_len
        else:
            tokens.append(("lit", data[pos]))
            advance = 1

        for i in range(pos, pos + advance):
            insert(i)
        pos += advance

    return tokens


def detokenize(tokens, dictionary):
    out = bytearray()
    patterns = dictionary.patterns
    for tok in tokens:
        kind = tok[0]
        if kind == "lit":
            out.append(tok[1])
        elif kind == "dict":
            out += patterns[tok[1]]
        else:  # ("match", length, distance) — byte-wise copy handles overlap
            length, distance = tok[1], tok[2]
            start = len(out) - distance
            for k in range(length):
                out.append(out[start + k])
    return bytes(out)
