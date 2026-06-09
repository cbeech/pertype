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

WINDOW = 1 << 19          # how far back an LZ match may reach (covers blob + file)
MIN_MATCH = 3             # shortest worthwhile match (dict or LZ)
MAX_MATCH = 1 << 12       # cap on a single match length
MAX_CHAIN = 128           # default / training hash-chain depth (also the adaptive floor)
ADAPT_MAX_CHAIN = 2048    # deepest adaptive parse (small inputs)
ADAPT_BUDGET = 2048 * 2048  # per-file work budget: depth ≈ BUDGET / size


def adaptive_max_chain(n):
    """Per-file hash-chain depth: deep on small inputs (where the ~1% optimal-parse gain
    lives and the absolute cost is tiny), tapering to ``MAX_CHAIN`` on large inputs (where a
    deep parse is expensive and the proportional gain is negligible). Always ``>= MAX_CHAIN``,
    so the result is never worse than the fixed default — a Pareto improvement with bounded
    cost. The parse cost scales with file size × depth, so ``BUDGET / size`` caps that product;
    matched byte-for-byte in the Rust port (`textcodec::adaptive_max_chain`)."""
    return max(MAX_CHAIN, min(ADAPT_MAX_CHAIN, ADAPT_BUDGET // max(n, 1)))

# Optional native acceleration of the LZ match-finder (the dominant cost of the
# cost-optimal parse). Imported lazily so the core stays zero-dependency; it
# returns the same integer candidate lists, so the DP below is unchanged and the
# produced tokens are identical.
_native = None


def _get_native():
    global _native
    if _native is None:
        try:
            from pertype import native as n
            _native = n if n.HAVE_NATIVE else False
        except Exception:
            _native = False
    return _native

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


def _match_len(buf, i, j, limit):
    """Common-prefix length of ``buf[i:]`` and ``buf[j:]``, capped at ``limit``.

    Galloping search: grow a power-of-two window by C-level slice comparison
    (memcmp) while it matches, then binary-search the failing window. Identical
    result to a byte-by-byte loop, but O(log L) comparisons instead of O(L) —
    the hot loop of LZ matching, so this is where pure-Python time is won.
    """
    n = 0
    step = 16
    while n + step <= limit and buf[i + n : i + n + step] == buf[j + n : j + n + step]:
        n += step
        step <<= 1
    hi = min(n + step, limit)              # first mismatch is in (n, hi]
    while n < hi:
        mid = (n + hi + 1) >> 1
        if buf[i + n : i + mid] == buf[j + n : j + mid]:
            n = mid
        else:
            hi = mid - 1
    return n


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
            length = _match_len(data, cand, pos, limit)
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


def _dict_matches(dictionary, combined, N, base, min_match):
    """Per-position longest dictionary match over ``combined[base:N]`` as plain-int
    lists ``(pid, length)``; pid is -1 where there is no usable match. Native when
    available, else the pure-Python ``dictionary.match`` loop — same result."""
    nat = _get_native()
    if nat:
        pid, ln = nat.dict_match_all(combined, base, min_match, dictionary.flat_index())
        return pid.tolist(), ln.tolist()
    pid = [-1] * (N - base)
    ln = [0] * (N - base)
    for p in range(base, N):
        dm = dictionary.match(combined, p, min_match)
        if dm is not None and dm[1] >= min_match:
            pid[p - base] = dm[0]
            ln[p - base] = dm[1]
    return pid, ln


def tokenize(data, dictionary, use_lz=True, prefix=b"", window=WINDOW,
             min_match=MIN_MATCH, max_match=MAX_MATCH, max_chain=MAX_CHAIN):
    n = len(data)
    nat = _get_native()

    if not use_lz:
        # Dictionary + literals only — no in-file back-references (prefix unused).
        if nat and min_match == 3:
            dpid, dlen = _dict_matches(dictionary, data, n, 0, min_match)
            tokens = []
            pos = 0
            while pos < n:
                if dlen[pos] >= min_match:
                    tokens.append(("dict", dpid[pos]))
                    pos += dlen[pos]
                else:
                    tokens.append(("lit", data[pos]))
                    pos += 1
            return tokens
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

    # Native fast path: precompute the per-position best LZ match (lz_best) and
    # dict match, then run the same lazy/greedy walk reading those arrays — no
    # per-position search. The arrays are integer-exact, so tokens are identical
    # to the Python walk below. lz_best inserts every position in order, matching
    # the walk's incremental hash-chain state.
    if nat and min_match == 3:
        best = nat.lz_best(combined, base, window, max_match, max_chain)
        if best is not None:
            bestl, bestd = best[0].tolist(), best[1].tolist()
            dpid, dlen = _dict_matches(dictionary, combined, N, base, min_match)

            def choice_at(at):
                i = at - base
                bl, bd, dl = bestl[i], bestd[i], dlen[i]
                lz_ok = _accept_lz(bl, bd)
                if dl >= min_match and (not lz_ok or bl < dl + _LZ_OVER_DICT_MARGIN):
                    return ("dict", dpid[i], dl)
                if lz_ok:
                    return ("match", bl, bd)
                return ("lit", combined[at], 1)

            tokens = []
            pos = base
            pending = None
            while pos < N:
                tok = pending if pending is not None else choice_at(pos)
                pending = None
                kind = tok[0]
                if kind == "lit":
                    tokens.append(("lit", combined[pos]))
                    pos += 1
                    continue
                if kind == "dict":
                    tokens.append(("dict", tok[1]))
                    pos += tok[2]
                    continue
                length = tok[1]
                if pos + 1 < N:
                    nxt = choice_at(pos + 1)
                    nxt_len = nxt[2] if nxt[0] == "dict" else (nxt[1] if nxt[0] == "match" else 0)
                    if nxt_len > length:
                        tokens.append(("lit", combined[pos]))
                        pending = nxt
                        pos += 1
                        continue
                tokens.append(("match", tok[1], tok[2]))
                pos += length
            return tokens

    # Pure-Python fallback: incremental hash chains + the same lazy walk.
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


def _forward(combined, N, base, window, max_match, max_chain, min_match):
    """LZ match-finder forward pass -> (off, cand_len, cand_dist) in CSR form.

    For data position ``p``, the candidate (length, distance) pairs are
    ``cand_len[off[p-base]:off[p-base+1]]`` and the parallel ``cand_dist``, in
    first-appearance order (smallest distance per distinct length). Uses the
    native finder when available (min_match must be 3, which all callers use);
    otherwise the pure-Python hash-chain search, producing the same structure.
    """
    nat = _get_native()
    if nat and min_match == 3:
        res = nat.lz_forward(combined, base, window, max_match, max_chain)
        if res is not None:
            return res

    head = {}
    prev = [-1] * N

    def insert(i):
        if i + min_match <= N:
            key = combined[i : i + min_match]
            prev[i] = head.get(key, -1)
            head[key] = i

    for i in range(base):
        insert(i)

    off = [0] * (N - base + 1)
    cand_len, cand_dist = [], []
    for p in range(base, N):
        off[p - base] = len(cand_len)
        found = {}
        cand = head.get(combined[p : p + min_match], -1)
        chain = max_chain
        limit = min(max_match, N - p)
        while cand != -1 and p - cand <= window and chain > 0:
            length = _match_len(combined, cand, p, limit)
            if length >= min_match:
                dist = p - cand
                if length not in found or dist < found[length]:
                    found[length] = dist
            cand = prev[cand]
            chain -= 1
        for length, dist in found.items():
            cand_len.append(length)
            cand_dist.append(dist)
        insert(p)
    off[N - base] = len(cand_len)
    return off, cand_len, cand_dist


def tokenize_optimal(data, dictionary, costs, prefix=b"", window=WINDOW,
                     min_match=MIN_MATCH, max_match=MAX_MATCH, max_chain=MAX_CHAIN):
    """Minimum-cost parse via dynamic programming.

    ``costs`` is ``(lit_cost, dict_cost, match_cost)`` — callables returning the
    bit cost of each token kind under the model. A backward pass computes, for
    every position, the cheapest way to encode the rest of the file, choosing
    among a literal, a dictionary reference, and the maximal LZ match at each
    distance. This finds globally cheaper parses than greedy/lazy (e.g. taking a
    nearer shorter match, or a literal, when it leads to a cheaper continuation).
    """
    lit_cost, dict_cost, match_cost = costs
    base = len(prefix)
    combined = prefix + data if base else data
    N = len(combined)

    # Native fast path: forward match-finding, dict matching and the backward DP
    # all in C. Match cost depends only on (length-slot, distance-slot), so a tiny
    # lookup table built by probing the cost callables reproduces it exactly — no
    # model access needed, and the DP's double arithmetic is bit-identical, so the
    # tokens match the Python parse below.
    nat = _get_native()
    if nat and min_match == 3:
        fwd = nat.lz_forward_arr(combined, base, window, max_match, max_chain)
        if fwd is not None:
            import numpy as np
            off, clen, cdist = fwd
            dpid, dlen = nat.dict_match_all(combined, base, min_match,
                                            dictionary.flat_index())
            lit_table = np.array([lit_cost(b) for b in range(256)], dtype=np.float64)
            ndict = len(dictionary.patterns)
            dict_table = np.array([dict_cost(pid) for pid in range(ndict)],
                                  dtype=np.float64)
            ND = MAX_DIST_SLOT + 1
            mc = np.empty((MAX_LEN_SLOT + 1) * ND, dtype=np.float64)
            for ls in range(MAX_LEN_SLOT + 1):
                length = value_from(ls, 0) + min_match - 1
                for ds in range(ND):
                    mc[ls * ND + ds] = match_cost(length, value_from(ds, 0))
            kind, aval, bval = nat.lz_dp(combined, base, off, clen, cdist, dpid, dlen,
                                         lit_table, dict_table, mc, ND, min_match)
            kl, al, bl = kind.tolist(), aval.tolist(), bval.tolist()
            tokens = []
            for i in range(len(kl)):
                k = kl[i]
                if k == 0:
                    tokens.append(("lit", al[i]))
                elif k == 1:
                    tokens.append(("dict", al[i]))
                else:
                    tokens.append(("match", al[i], bl[i]))
            return tokens

    # Forward pass: per data position, the smallest distance achieving each
    # maximal match length, flattened CSR-style — candidates for position p are
    # ``cand_len[off[p-base] : off[p-base+1]]`` (and the parallel ``cand_dist``),
    # in first-appearance order. Native when available; the Python fallback below
    # produces the identical structure.
    off, cand_len, cand_dist = _forward(combined, N, base, window, max_match,
                                        max_chain, min_match)

    # Per-position longest dictionary match (native when available).
    dpid, dlen = _dict_matches(dictionary, combined, N, base, min_match)

    # Backward pass: cheapest cost to encode combined[p:].
    cost_to_end = [0.0] * (N + 1)
    choice = [None] * (N + 1)
    for p in range(N - 1, base - 1, -1):
        best = lit_cost(combined[p]) + cost_to_end[p + 1]
        best_choice = ("lit", combined[p])

        pi = p - base
        if dlen[pi] >= min_match:
            c = dict_cost(dpid[pi]) + cost_to_end[p + dlen[pi]]
            if c < best:
                best, best_choice = c, ("dict", dpid[pi], dlen[pi])

        for idx in range(off[pi], off[pi + 1]):
            length, dist = cand_len[idx], cand_dist[idx]
            c = match_cost(length, dist) + cost_to_end[p + length]
            if c < best:
                best, best_choice = c, ("match", length, dist)

        cost_to_end[p] = best
        choice[p] = best_choice

    # Walk the chosen path forward.
    tokens = []
    p = base
    while p < N:
        ch = choice[p]
        if ch[0] == "lit":
            tokens.append(("lit", ch[1]))
            p += 1
        elif ch[0] == "dict":
            tokens.append(("dict", ch[1]))
            p += ch[2]
        else:
            tokens.append(("match", ch[1], ch[2]))
            p += ch[1]
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
