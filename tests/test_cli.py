"""The product CLI: the unified ``compress`` / ``decompress`` flow must just work and
round-trip byte-exact, routing itself via the self-describing container."""
import pytest

from pertype import cli
from pertype.model import train


def _run(*argv):
    cli.main(list(argv))


def test_unified_auto_roundtrip(tmp_path):
    # no model -> auto-router, self-describing .cmp, decompress needs no flags
    src = tmp_path / "data.csv"
    src.write_bytes(b"t;v;n\n" + b"".join(b"2024-01-01;%d.50;%d\n" % (i, i) for i in range(200)))
    cmp_path = tmp_path / "data.cmp"
    _run("compress", str(src), "-o", str(cmp_path))
    out = tmp_path / "data.out"
    _run("decompress", str(cmp_path), "-o", str(out))
    assert out.read_bytes() == src.read_bytes()


def test_unified_trained_roundtrip_and_routing(tmp_path):
    # a small trained model; --model is tried and (on type-matched data) wins
    samples = [b'{"name":"item%d","value":%d,"ok":true}' % (i, i * 7) for i in range(60)]
    model = train(samples, type_id="json", max_patterns=128)
    mpath = tmp_path / "json.model"
    mpath.write_bytes(model.save())

    src = tmp_path / "page.json"
    src.write_bytes(samples[3])
    cmp_path = tmp_path / "page.cmp"
    _run("compress", str(src), "--model", str(mpath), "-o", str(cmp_path))

    # decompressing a trained-model container without --model fails helpfully
    with pytest.raises(SystemExit):
        _run("decompress", str(cmp_path))

    out = tmp_path / "page.out"
    _run("decompress", str(cmp_path), "--model", str(mpath), "-o", str(out))
    assert out.read_bytes() == src.read_bytes()


def test_identify_runs(tmp_path, capsys):
    src = tmp_path / "x.json"
    src.write_bytes(b'{"a":1,"b":[2,3]}')
    _run("identify", str(src))
    assert "->" in capsys.readouterr().out
