"""End-to-end codec tests — the lossless guarantee."""
import os

from pertype.codec import compress, decompress
from pertype.model import train


def _model():
    samples = [b'{"name":"item%d","value":%d,"ok":true}' % (i, i * 7) for i in range(80)]
    return train(samples, type_id="json", max_patterns=256), samples


def test_roundtrip_training_like_data():
    m, samples = _model()
    for s in samples:
        assert decompress(compress(s, m), m) == s


def test_roundtrip_empty():
    m, _ = _model()
    assert decompress(compress(b"", m), m) == b""


def test_roundtrip_random_bytes():
    m, _ = _model()
    for _ in range(30):
        data = os.urandom(500)
        assert decompress(compress(data, m), m) == data


def test_roundtrip_bytes_never_seen_in_training():
    m, _ = _model()
    data = bytes(range(256)) * 4
    assert decompress(compress(data, m), m) == data


def test_numeric_transform_roundtrips_and_is_selected():
    # 16-bit little-endian smooth-ish data: training should pick a decorrelating
    # transform, and compress/decompress must still be byte-exact.
    samples = [b"".join(((i * 5 + k) & 0xFFFF).to_bytes(2, "little")
                        for i in range(400)) for k in range(40)]
    m = train(samples, type_id="num", max_patterns=256)
    assert m.transform != (), "expected a non-identity transform for numeric data"
    data = b"".join(((i * 5 + 7) & 0xFFFF).to_bytes(2, "little") for i in range(400))
    assert decompress(compress(data, m), m) == data


def test_repeat_offset_data_roundtrips():
    # Fixed-stride repeated records reuse the same match distance over and over,
    # which is exactly what repeat-offset modeling targets. Must round-trip.
    samples = [b"record %04d | name=%s | status=ok\n" % (i, b"abcdef") for i in range(120)]
    m = train(samples, type_id="logs", max_patterns=256)
    data = b"".join(b"record %04d | name=zzzzzz | status=ok\n" % i for i in range(40))
    assert decompress(compress(data, m), m) == data


def test_compresses_structured_data():
    m, _ = _model()
    data = b'{"name":"itemXYZ","value":12345,"ok":true}' * 20
    out = compress(data, m)
    assert len(out) < len(data)


def test_checksum_detects_corruption():
    m, _ = _model()
    # Long, repetitive payload so a mid-stream flip lands in consumed bits.
    data = b'{"name":"hello","value":1,"ok":true}' * 50
    blob = bytearray(compress(data, m))
    blob[len(blob) // 2] ^= 0xFF  # flip bits in the middle of the payload
    try:
        decompress(bytes(blob), m)
        corrupted_undetected = True
    except ValueError:
        corrupted_undetected = False
    assert not corrupted_undetected


def test_wrong_model_rejected():
    m, _ = _model()
    other = train([b"totally different corpus of words"], type_id="logs")
    data = b'{"name":"x","value":1,"ok":true}'
    blob = compress(data, m)
    try:
        decompress(blob, other)
        rejected = False
    except ValueError:
        rejected = True
    assert rejected
