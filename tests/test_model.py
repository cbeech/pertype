"""Tests for model training and serialization."""
from compressor.model import Model, train


def _corpus():
    return [b'{"name":"alice","age":%d}' % i for i in range(60)]


def test_train_produces_dictionary_and_models():
    m = train(_corpus(), type_id="json", max_patterns=128)
    assert m.dictionary.patterns
    assert m.main_model.total > 0
    assert m.dist_model.total > 0
    assert m.type_id == "json"


def test_save_load_roundtrip():
    m = train(_corpus(), type_id="json", max_patterns=128)
    blob = m.save()
    m2 = Model.load(blob)
    assert m2.type_id == m.type_id
    assert m2.version == m.version
    assert m2.use_lz == m.use_lz
    assert m2.dictionary.patterns == m.dictionary.patterns
    assert m2.main_model.freqs == m.main_model.freqs
    assert m2.dist_model.freqs == m.dist_model.freqs


def test_lz_decision_is_a_bool():
    m = train(_corpus(), type_id="json", max_patterns=128)
    assert isinstance(m.use_lz, bool)


def test_every_byte_value_is_codeable():
    # The model must be able to encode any byte (literal fallback), so all 256
    # literal symbols must be present even if absent from the corpus.
    m = train(_corpus(), type_id="json", max_patterns=64)
    for b in range(256):
        assert b in m.main_model.index
