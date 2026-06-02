"""Compress/decompress a single file against a trained model.

Symbols are coded with an arithmetic coder driven by the model's frequency
models; the "extra bits" of length/distance slots are coded as raw bits through
the same coder. Token stream (per token):
  * literal       -> main symbol = byte (0..255)
  * dict ref      -> main symbol = 256 + pattern_id
  * LZ match      -> main symbol = LEN_BASE + length_slot, then ``length_slot``
                     extra bits, then a distance symbol from the distance model,
                     then ``dist_slot`` extra bits.

Container layout (all integers big-endian)::

    magic     "CZ"        2 bytes
    fmt_ver   u8
    tid_len   u8
    type_id   bytes       must match the model used to decompress
    model_ver u16         must match
    orig_len  u64
    n_tokens  u32         number of main symbols in the payload
    crc32     u32         CRC of the original data
    payload   bytes       bit-packed, MSB-first

The model is referenced by (type_id, model_ver), not embedded — its dictionary
and tables are shipped once and amortized across every file of the type.
"""
import zlib

from compressor.arithmetic import ArithmeticEncoder, ArithmeticDecoder
from compressor.tokenizer import (
    MIN_MATCH, detokenize, tokenize, tokenize_optimal, value_from, value_slot,
)

MAGIC = b"CZ"
FMT_VERSION = 3


def _encode_tokens(tokens, model, enc):
    main, dist, len_base = model.main_model, model.dist_model, model.len_base
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
            dslot, dextra = value_slot(distance)
            dist.encode(enc, dslot)
            enc.encode_bits(dextra, dslot)


def _decode_tokens(dec, model, n_tokens):
    main, dist, len_base = model.main_model, model.dist_model, model.len_base
    n_patterns = len(model.dictionary.patterns)
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
            dslot = dist.decode(dec)
            distance = value_from(dslot, dec.decode_bits(dslot))
            tokens.append(("match", length, distance))
    return tokens


def compress(data, model):
    if model.use_lz:
        tokens = tokenize_optimal(data, model.dictionary, model.costs(), prefix=model.blob)
    else:
        tokens = tokenize(data, model.dictionary, use_lz=False)
    enc = ArithmeticEncoder()
    _encode_tokens(tokens, model, enc)
    enc.finish()
    payload = enc.getvalue()

    tid = model.type_id.encode("utf-8")
    header = bytearray()
    header += MAGIC
    header += bytes([FMT_VERSION])
    header += bytes([len(tid)])
    header += tid
    header += model.version.to_bytes(2, "big")
    header += len(data).to_bytes(8, "big")
    header += len(tokens).to_bytes(4, "big")
    header += (zlib.crc32(data) & 0xFFFFFFFF).to_bytes(4, "big")
    return bytes(header) + payload


def decompress(blob, model):
    if blob[:2] != MAGIC:
        raise ValueError("not a CZ container")
    pos = 2
    fmt_ver = blob[pos]
    pos += 1
    if fmt_ver != FMT_VERSION:
        raise ValueError(f"unsupported container format version {fmt_ver}")
    tid_len = blob[pos]
    pos += 1
    type_id = blob[pos : pos + tid_len].decode("utf-8")
    pos += tid_len
    model_ver = int.from_bytes(blob[pos : pos + 2], "big")
    pos += 2
    if type_id != model.type_id or model_ver != model.version:
        raise ValueError(
            f"model mismatch: container is {type_id} v{model_ver}, "
            f"model is {model.type_id} v{model.version}"
        )
    orig_len = int.from_bytes(blob[pos : pos + 8], "big")
    pos += 8
    n_tokens = int.from_bytes(blob[pos : pos + 4], "big")
    pos += 4
    crc = int.from_bytes(blob[pos : pos + 4], "big")
    pos += 4

    try:
        dec = ArithmeticDecoder(blob[pos:])
        tokens = _decode_tokens(dec, model, n_tokens)
        data = detokenize(tokens, model.dictionary, prefix=model.blob)
    except (EOFError, ValueError, IndexError) as exc:
        raise ValueError(f"corrupt payload: {exc}") from exc

    if len(data) != orig_len:
        raise ValueError("length mismatch — corrupt or wrong model")
    if (zlib.crc32(data) & 0xFFFFFFFF) != crc:
        raise ValueError("checksum mismatch — corrupt data")
    return data
