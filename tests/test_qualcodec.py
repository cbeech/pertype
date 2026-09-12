"""Round-trip tests for the FASTQ quality-score context codec."""
import random

import pytest

from pertype import auto, qualcodec
from pertype import native


def _drift_reads(rng, nreads, alpha=(33, 126), drift=(-2, 1)):
    reads = []
    for _ in range(nreads):
        L = rng.choice([1, 10, 63, 64, 100, 200])
        q = rng.randint(*alpha)
        read = []
        for _ in range(L):
            if rng.random() < 0.02:
                q = rng.randint(*alpha)
            q = max(33, min(126, q + rng.randint(*drift)))
            read.append(q)
        reads.append(bytes(read))
    return reads


def _roundtrip(reads):
    qual = b"".join(reads)
    lengths = [len(r) for r in reads]
    blob = qualcodec.encode(qual, lengths)
    assert qualcodec.decode(blob) == (qual, lengths)
    return blob


def test_empty():
    blob = qualcodec.encode(b"", [])
    assert qualcodec.decode(blob) == (b"", [])


def test_single_level_alphabet():
    # one Phred value only ('I' — the classic Illumina plateau)
    _roundtrip([b"I" * 150] * 20)


def test_full_range_alphabet():
    rng = random.Random(1)
    reads = _drift_reads(rng, 60, alpha=(33, 126))
    reads.append(bytes(range(33, 127)))            # every Phred symbol in one read
    _roundtrip(reads)


def test_read_lengths_span_position_cap():
    # reads of 1..200 exercise p both below and at the 63 clamp within one stream
    rng = random.Random(2)
    _roundtrip(_drift_reads(rng, 120))


def test_rescale_exercised():
    # one constant context well past RESCALE (INCR=24 -> ~683 symbols per halving)
    _roundtrip([b"5" * 5000])


def test_native_byte_identical():
    if not native.HAVE_NATIVE:
        pytest.skip("native library not built (gcc unavailable)")
    rng = random.Random(3)
    for reads in ([b"I" * 100] * 10, _drift_reads(rng, 80), [bytes(range(33, 127))] * 4):
        qual = b"".join(reads)
        lengths = [len(r) for r in reads]
        assert qualcodec.encode(qual, lengths) == qualcodec._encode_py(qual, lengths)
        blob = qualcodec._encode_py(qual, lengths)
        assert qualcodec.decode(blob) == qualcodec._decode_py(blob)


def test_fastq_file_routes_through_auto():
    # 150 bp reads, quality drifting high->low along the read (Illumina-like) —
    # enough quality entropy that the context codec beats plain deflate
    rng = random.Random(4)
    recs = []
    for i in range(120):
        q = 38.0
        read = []
        for _ in range(150):
            q += rng.gauss(-0.06, 0.8)
            read.append(int(max(2, min(40, round(q)))) + 33)
        seq = b"".join(bytes([rng.choice(b"ACGT")]) for _ in range(150))
        recs += [b"@READ:%d" % i, seq, b"+", bytes(read)]
    data = b"\n".join(recs) + b"\n"
    blob = auto.auto_compress(data)
    assert auto.auto_decompress(blob) == data          # byte-exact guarantee
    assert auto.method_name(blob) == "fastq->qualcodec"
    # the candidate route itself is wired and provably reversible
    assert auto._fastq_decode(auto._try_fastq(data)) == data
