"""Round-trip tests for pertype.fastqcodec (FQS1 whole-file FASTQ codec)."""
import random

import pytest

from pertype import fastqcodec


def _fq(records, trailing=True):
    parts = []
    for h, s, p, q in records:
        parts += [h, s, p, q]
    return b"\n".join(parts) + (b"\n" if trailing else b"")


def _gen(nrec, seed=1, len_lo=30, len_hi=80):
    rng = random.Random(seed)
    recs = []
    for i in range(nrec):
        L = rng.randint(len_lo, len_hi)
        seq = "".join(rng.choice("ACGTN") for _ in range(L))
        qual = bytes(rng.randint(33, 74) for _ in range(L))
        recs.append((b"@INST.1.%d %d:N:0:ACGT" % (i + 1, i + 1), seq.encode(), b"+", qual))
    return recs


def test_roundtrip_basic():
    data = _fq(_gen(50))
    assert fastqcodec.decode(fastqcodec.encode(data)) == data


def test_roundtrip_no_trailing_newline():
    data = _fq(_gen(20), trailing=False)
    assert fastqcodec.decode(fastqcodec.encode(data)) == data


def test_roundtrip_single_record():
    data = _fq([(b"@r1", b"ACGTN", b"+", b"#$%&!")])
    assert fastqcodec.decode(fastqcodec.encode(data)) == data


def test_roundtrip_header_exception():
    recs = _gen(10)
    recs[5] = (b"@COMPLETELY different header!", recs[5][1], b"+", recs[5][3])
    data = _fq(recs)
    assert fastqcodec.decode(fastqcodec.encode(data)) == data


def test_roundtrip_plus_with_content():
    recs = [(h, s, b"+" + h[1:], q) for h, s, _, q in _gen(10)]
    data = _fq(recs)
    assert fastqcodec.decode(fastqcodec.encode(data)) == data


def test_roundtrip_all_n_run():
    recs = [(b"@n1", b"N" * 40, b"+", b"F" * 40),
            (b"@n2", b"NNACGTNN", b"+", b"FFFFFFFF")]
    data = _fq(recs)
    assert fastqcodec.decode(fastqcodec.encode(data)) == data


def test_deterministic():
    data = _fq(_gen(30))
    assert fastqcodec.encode(data) == fastqcodec.encode(data)


def test_reject_odd_lines():
    with pytest.raises(ValueError):
        fastqcodec.encode(b"@h\nACGT\n+\n")


def test_reject_length_mismatch():
    with pytest.raises(ValueError):
        fastqcodec.encode(b"@h\nACGT\n+\nFFF\n")


def test_reject_bad_quality():
    with pytest.raises(ValueError):
        fastqcodec.encode(b"@h\nACGT\n+\n\x01\x02\x03\x04\n")


def test_reject_not_fqs1():
    with pytest.raises(ValueError):
        fastqcodec.decode(b"XXXX" + b"\x00" * 20)


def test_strand_heavy_reads():
    # reads drawn from both strands of a small genome: exercises orientation
    rng = random.Random(7)
    genome = "".join(rng.choice("ACGT") for _ in range(3000))
    comp = str.maketrans("ACGT", "TGCA")
    recs = []
    for i in range(60):
        st = rng.randrange(len(genome) - 60)
        s = genome[st:st + 60]
        if rng.random() < 0.5:
            s = s.translate(comp)[::-1]
        recs.append((b"@g.%d" % i, s.encode(), b"+", b"F" * 60))
    data = _fq(recs)
    assert fastqcodec.decode(fastqcodec.encode(data)) == data
