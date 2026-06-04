"""Round-trip tests for the shared 2D intra predictors (MED / Paeth)."""
import numpy as np

from compressor import predictors


def _check(arr, kind):
    res = predictors.forward(arr.astype(np.int32), kind)
    rec = predictors.reconstruct(res, kind)
    assert np.array_equal(rec, arr.astype(np.int32)), (kind, arr.shape)


def test_roundtrip_shapes():
    rng = np.random.default_rng(0)
    for kind in ("med", "paeth"):
        for shape in [(1, 1), (1, 17), (17, 1), (29, 31), (64, 48)]:
            _check(rng.integers(0, 256, shape), kind)


def test_roundtrip_gradient_and_flat():
    g = (np.add.outer(np.arange(40), np.arange(50)) % 256).astype(np.int32)
    flat = np.full((20, 20), 200, dtype=np.int32)
    for kind in ("med", "paeth"):
        _check(g, kind)
        _check(flat, kind)


def test_predictors_decorrelate_a_gradient():
    # a smooth ramp should leave near-zero residuals under both predictors
    g = (np.add.outer(np.arange(64), np.arange(64))).astype(np.int32)
    for kind in ("med", "paeth"):
        res = predictors.forward(g, kind)
        assert np.abs(res).mean() < 2.0          # vs raw spread of ~32
