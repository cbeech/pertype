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

WINDOW = 1 << 18          # how far back an LZ match may reach (covers blob + file)
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


def _find_lz(data, pos, head, prev, window, max_match, max_chain):
    """Longest in-file match at ``pos`` via hash chains over positions < pos.

    Equal-length matches keep the smallest distance (chains run newest-first).
    Returns ``(length, distance)`` with length 0 if nothing qualifies.
    """
    n = len(data)
    best_len, best_dist = 0, 0
    if pos + MIN_MATCH <= n:
        cand = head.get(data[pos : pos + MIN_MATCH], -1)
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
    return best_len, best_dist


def _decide(data, pos, dictionary, best_len, best_dist):
    """Pick the token for ``pos`` from the LZ match and the dictionary match.

    Returns one of ``("dict", pid, length)``, ``("match", length, distance)``,
    or ``("lit", byte, 1)`` — the third element is always the covered length.
    """
    dm = dictionary.match(data, pos, MIN_MATCH)
    dict_len = dm[1] if dm else 0
    use_dict = dict_len >= MIN_MATCH
    lz_ok = _accept_lz(best_len, best_dist)

    if use_dict and (not lz_ok or best_len < dict_len + _LZ_OVER_DICT_MARGIN):
        return ("dict", dm[0], dict_len)
    if lz_ok:
        return ("match", best_len, best_dist)
    return ("lit", data[pos], 1)


def tokenize(data, dictionary, use_lz=True, prefix=b"", window=WINDOW,
             min_match=MIN_MATCH, max_match=MAX_MATCH, max_chain=MAX_CHAIN):
    n = len(data)

    if not use_lz:
        # Dictionary + literals only — no in-file back-references (prefix unused).
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

    # ``prefix`` is a trained dictionary blob prepended to the file's history, so
    # LZ matches can reach into arbitrary substrings of it. We tokenize over the
    # combined buffer but only emit tokens for the data region; distances run in
    # combined-buffer space, which the decoder reproduces by seeding its output
    # with the same prefix.
    base = len(prefix)
    combined = prefix + data if base else data
    N = len(combined)

    tokens = []
    head = {}                 # 3-byte key -> most recent position
    prev = [-1] * N           # position -> previous position with same 3 bytes

    def insert(i):
        if i + min_match <= N:
            key = combined[i : i + min_match]
            prev[i] = head.get(key, -1)
            head[key] = i

    for i in range(base):     # preload the blob so the data can reference it
        insert(i)

    def choice(at):
        bl, bd = _find_lz(combined, at, head, prev, window, max_match, max_chain)
        return _decide(combined, at, dictionary, bl, bd)

    # Lazy matching: when an LZ match is found, peek one byte ahead — if the next
    # position yields a strictly longer match, emit a literal now and take the
    # better match next. Dictionary matches commit immediately (cheapest token).
    pos = base
    pending = None            # token already computed for the current pos
    while pos < N:
        tok = pending if pending is not None else choice(pos)
        pending = None
        kind = tok[0]

        if kind == "lit":
            tokens.append(("lit", combined[pos]))
            insert(pos)
            pos += 1
            continue

        if kind == "dict":
            length = tok[2]
            tokens.append(("dict", tok[1]))
            for i in range(pos, pos + length):
                insert(i)
            pos += length
            continue

        # LZ match — try the lazy one-byte lookahead.
        length = tok[1]
        insert(pos)                          # so the lookahead at pos+1 sees pos
        if pos + 1 < N:
            nxt = choice(pos + 1)
            nxt_len = nxt[2] if nxt[0] == "dict" else (nxt[1] if nxt[0] == "match" else 0)
            if nxt_len > length:
                tokens.append(("lit", combined[pos]))
                pending = nxt
                pos += 1
                continue

        tokens.append(("match", tok[1], tok[2]))
        for i in range(pos + 1, pos + length):
            insert(i)
        pos += length

    return tokens


def detokenize(tokens, dictionary, prefix=b""):
    out = bytearray(prefix)
    base = len(prefix)
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
    return bytes(out[base:])
