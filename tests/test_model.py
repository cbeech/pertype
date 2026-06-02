"""Tests for model training and serialization."""
from compressor.model import BLOB_CAP, Model, _build_blob, train


def test_blob_contains_frequent_content_and_respects_cap():
    # "<BOILERPLATE>" recurs in every sample; random tails do not.
    samples = [b"<BOILERPLATE-HEADER-BLOCK>tail%05d_padding_padding_padding" % i
               for i in range(500)]
    blob = _build_blob(samples, cap=4096)
    assert len(blob) <= 4096
    assert b"<BOILERPLATE-HEADER-BLOCK>" in blob


def test_blob_small_corpus_returned_whole():
    samples = [b"abc", b"def"]
    assert _build_blob(samples, cap=4096) == b"abcdef"


def test_blob_empty_corpus():
    assert _build_blob([], cap=4096) == b""


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
    assert m2.blob == m.blob
    assert m2.dictionary.patterns == m.dictionary.patterns
    assert m2.main_model.freqs == m.main_model.freqs
    assert m2.dist_model.freqs == m.dist_model.freqs
    assert m2.mode_model.freqs == m.mode_model.freqs


def test_lz_decision_is_a_bool():
    m = train(_corpus(), type_id="json", max_patterns=128)
    assert isinstance(m.use_lz, bool)


def test_every_byte_value_is_codeable():
    # The model must be able to encode any byte (literal fallback), so all 256
    # literal symbols must be present even if absent from the corpus.
    m = train(_corpus(), type_id="json", max_patterns=64)
    for b in range(256):
        assert b in m.main_model.index
