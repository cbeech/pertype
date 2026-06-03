"""Round-trip tests for the context-adaptive residual coder."""
import random

from compressor import ctxcoder


def _roundtrip(res):
    blob = ctxcoder.encode(res)
    assert ctxcoder.decode(blob, len(res)) == list(res)


def test_empty():
    _roundtrip([])


def test_all_zeros():
    _roundtrip([0] * 1000)


def test_signs_and_zero():
    _roundtrip([0, 1, -1, 2, -2, 7, -7, 255, -256])


def test_single_value():
    _roundtrip([42])
    _roundtrip([-42])


def test_large_magnitudes():
    _roundtrip([0, 1 << 20, -(1 << 20), (1 << 40), -(1 << 40)])


def test_random_small():
    rng = random.Random(1)
    _roundtrip([rng.randint(-8, 8) for _ in range(5000)])


def test_random_mixed_activity():
    # alternating quiet / loud blocks — exercises the context switching
    rng = random.Random(2)
    res = []
    for _ in range(40):
        scale = rng.choice([1, 4, 64, 1024])
        res += [rng.randint(-scale, scale) for _ in range(200)]
    _roundtrip(res)


def test_beats_fixed_cost_on_varying_signal():
    # a signal with bursts should cost clearly less than 8 bits/sample
    rng = random.Random(3)
    res = []
    for _ in range(50):
        scale = rng.choice([1, 2])      # mostly tiny residuals
        res += [rng.randint(-scale, scale) for _ in range(200)]
    blob = ctxcoder.encode(res)
    assert len(blob) * 8 / len(res) < 4.0
