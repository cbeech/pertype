"""End-to-end codec tests — the lossless guarantee."""
import os

from compressor.codec import compress, decompress
from compressor.model import train


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
