"""Compress/decompress a single file against a trained model.

Symbols are coded with an arithmetic coder driven by the model's frequency
models; the "extra bits" of length/distance slots are coded as raw bits through
the same coder. Token stream (per token):
  * literal       -> main symbol = byte (0..255)
  * dict ref      -> main symbol = 256 + pattern_id
  * LZ match      -> main symbol = LEN_BASE + length_slot, then ``length_slot``
                     extra bits, then a distance symbol from the distance model,
                     then ``dist_slot`` extra bits.

Container layout (compact — the per-file overhead matters for the many-small-files
win, so lengths are LEB128 varints and the model identity is a 2-byte hash rather
than the full type-id string)::

    magic     u8          0xC7
    fmt_ver   u8
    id_hash   u16         hash of (type_id, model_ver); must match the model used
    orig_len  varint      original byte length
    n_tokens  varint      number of main symbols in the payload
    crc32     u32         CRC of the original data
    payload   bytes       bit-packed, MSB-first

The model is referenced by its identity hash, not embedded — its dictionary and
tables are shipped once and amortized across every file of the type. A wrong
model fails the id_hash check (and then the length/CRC checks) on decompress.
"""
import zlib

from pertype import transform
from pertype.arithmetic import ArithmeticEncoder, ArithmeticDecoder
from pertype.model import MODE_NORMAL, REP_INIT
from pertype.tokenizer import (
    MIN_MATCH, adaptive_max_chain, detokenize, tokenize, tokenize_optimal, value_from,
    value_slot,
)

MAGIC = 0xC7
FMT_VERSION = 5


def _write_varint(buf, n):
    """Append ``n`` (unsigned) to ``buf`` as LEB128."""
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            buf.append(b | 0x80)
        else:
            buf.append(b)
            return


def _read_varint(blob, pos):
    """Read a LEB128 unsigned int from ``blob`` at ``pos``; return (value, new_pos)."""
    val = 0
    shift = 0
    while True:
        b = blob[pos]
        pos += 1
        val |= (b & 0x7F) << shift
        if not (b & 0x80):
            return val, pos
        shift += 7


def _id_hash(type_id, version):
    """A 2-byte identity for (type_id, model_ver) — a wrong model fails this."""
    return zlib.crc32(f"{type_id}:{version}".encode("utf-8")) & 0xFFFF

# Optional native acceleration of the per-symbol arithmetic loop. Imported lazily
# so the core stays zero-dependency; byte-identical to the Python path below, so
# files are interchangeable across both.
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


def _contig_cum(fm):
    """The model's prefix sums if its alphabet is contiguous 0..n-1 (then symbol
    == index, which the native coder relies on); else None."""
    s = fm.symbols
    return fm.cum if s and s[0] == 0 and s[-1] == len(s) - 1 else None


def _native_cums(model):
    cums = (_contig_cum(model.main_model), _contig_cum(model.dist_model),
            _contig_cum(model.mode_model))
    return cums if all(c is not None for c in cums) else None


def _encode_tokens(tokens, model, enc):
    main, dist, mode, len_base = (
        model.main_model, model.dist_model, model.mode_model, model.len_base
    )
    reps = list(REP_INIT)  # repeat-offset cache, in lockstep with the decoder
    for tok in tokens:
        kind = tok[0]
        if kind == "lit":
            main.encode(enc, tok[1])
        elif kind == "dict":
            main.encode(enc, 256 + tok[1])
        else:  # ("match", length, distance)
            length, distance = tok[1], tok[2]
            lslot, lextra = value_slot(length - MIN_MATCH + 1)
            main.encode(enc, len_base + lslot)
            enc.encode_bits(lextra, lslot)
            if distance in reps:
                i = reps.index(distance)
                mode.encode(enc, i + 1)        # reuse a cached distance — no dist code
                reps.pop(i)
            else:
                mode.encode(enc, MODE_NORMAL)
                dslot, dextra = value_slot(distance)
                dist.encode(enc, dslot)
                enc.encode_bits(dextra, dslot)
                reps.pop()
            reps.insert(0, distance)


def _decode_tokens(dec, model, n_tokens):
    main, dist, mode, len_base = (
        model.main_model, model.dist_model, model.mode_model, model.len_base
    )
    n_patterns = len(model.dictionary.patterns)
    reps = list(REP_INIT)
    tokens = []
    for _ in range(n_tokens):
        sym = main.decode(dec)
        if sym < 256:
            tokens.append(("lit", sym))
        elif sym < 256 + n_patterns:
            tokens.append(("dict", sym - 256))
        else:
            lslot = sym - len_base
            length = value_from(lslot, dec.decode_bits(lslot)) + MIN_MATCH - 1
            m = mode.decode(dec)
            if m == MODE_NORMAL:
                dslot = dist.decode(dec)
                distance = value_from(dslot, dec.decode_bits(dslot))
                reps.pop()
            else:
                distance = reps[m - 1]
                reps.pop(m - 1)
            reps.insert(0, distance)
            tokens.append(("match", length, distance))
    return tokens


def _encode_payload(tokens, model):
    """Arithmetic-code the token stream; native loop if available, else Python."""
    nat = _get_native()
    cums = _native_cums(model) if nat else None
    if cums is not None:
        import numpy as np
        n = len(tokens)
        kind = np.empty(n, dtype=np.int32)
        aval = np.empty(n, dtype=np.int64)
        bval = np.zeros(n, dtype=np.int64)
        for i, tok in enumerate(tokens):
            t = tok[0]
            if t == "lit":
                kind[i] = 0; aval[i] = tok[1]
            elif t == "dict":
                kind[i] = 1; aval[i] = tok[1]
            else:
                kind[i] = 2; aval[i] = tok[1]; bval[i] = tok[2]
        return nat.lz_encode(kind, aval, bval, cums[0], cums[1], cums[2],
                             model.len_base, MIN_MATCH)
    enc = ArithmeticEncoder()
    _encode_tokens(tokens, model, enc)
    enc.finish()
    return enc.getvalue()


def _decode_payload(payload, model, n_tokens):
    nat = _get_native()
    cums = _native_cums(model) if nat else None
    if cums is not None:
        n_patterns = len(model.dictionary.patterns)
        kind, aval, bval = nat.lz_decode(payload, n_tokens, cums[0], cums[1],
                                         cums[2], model.len_base, n_patterns, MIN_MATCH)
        kl, al, bl = kind.tolist(), aval.tolist(), bval.tolist()
        tokens = []
        for i in range(n_tokens):
            k = kl[i]
            if k == 0:
                tokens.append(("lit", al[i]))
            elif k == 1:
                tokens.append(("dict", al[i]))
            else:
                tokens.append(("match", al[i], bl[i]))
        return tokens
    return _decode_tokens(ArithmeticDecoder(payload), model, n_tokens)


def compress(data, model, max_chain=None):
    # Decorrelate first; the rest of the pipeline encodes the transformed bytes.
    tdata = transform.apply(data, model.transform)
    if model.use_lz:
        # Adaptive parse depth by default: deep on small files (cheap, ~1% denser), shallow
        # on large ones (bounded cost). Always >= the fixed default, so never worse.
        mc = adaptive_max_chain(len(tdata)) if max_chain is None else max_chain
        tokens = tokenize_optimal(tdata, model.dictionary, model.costs(),
                                  prefix=model.blob, max_chain=mc)
    else:
        tokens = tokenize(tdata, model.dictionary, use_lz=False)
    payload = _encode_payload(tokens, model)

    header = bytearray()
    header.append(MAGIC)
    header.append(FMT_VERSION)
    header += _id_hash(model.type_id, model.version).to_bytes(2, "big")
    _write_varint(header, len(data))
    _write_varint(header, len(tokens))
    header += (zlib.crc32(data) & 0xFFFFFFFF).to_bytes(4, "big")
    return bytes(header) + payload


def decompress(blob, model):
    if not blob or blob[0] != MAGIC:
        raise ValueError("not a CZ container")
    fmt_ver = blob[1]
    if fmt_ver != FMT_VERSION:
        raise ValueError(f"unsupported container format version {fmt_ver}")
    id_hash = int.from_bytes(blob[2:4], "big")
    if id_hash != _id_hash(model.type_id, model.version):
        raise ValueError(
            f"model mismatch: container id_hash {id_hash:#06x}, "
            f"model is {model.type_id} v{model.version}"
        )
    pos = 4
    orig_len, pos = _read_varint(blob, pos)
    n_tokens, pos = _read_varint(blob, pos)
    crc = int.from_bytes(blob[pos : pos + 4], "big")
    pos += 4

    try:
        tokens = _decode_payload(blob[pos:], model, n_tokens)
        tdata = detokenize(tokens, model.dictionary, prefix=model.blob)
        data = transform.invert(tdata, model.transform)
    except (EOFError, ValueError, IndexError) as exc:
        raise ValueError(f"corrupt payload: {exc}") from exc

    if len(data) != orig_len:
        raise ValueError("length mismatch — corrupt or wrong model")
    if (zlib.crc32(data) & 0xFFFFFFFF) != crc:
        raise ValueError("checksum mismatch — corrupt data")
    return data
