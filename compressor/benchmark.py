"""Benchmark the trained codec against gzip and zstd on a held-out test set.

Layout expected::

    <root>/<type>/train/*   training files (build the model from these)
    <root>/<type>/test/*    held-out files (measure compression on these)

Train and test sets must be disjoint; ``load_split`` checks this by content hash
so a reported win can't come from having memorized the test data.
"""
import hashlib
import os
import subprocess
import tempfile

from compressor.codec import compress, decompress
from compressor.model import train


def _read_dir(path):
    files = []
    if not os.path.isdir(path):
        return files
    for name in sorted(os.listdir(path)):
        fp = os.path.join(path, name)
        if os.path.isfile(fp):
            with open(fp, "rb") as fh:
                files.append((fp, fh.read()))
    return files


def load_split(root, type_id):
    train_files = _read_dir(os.path.join(root, type_id, "train"))
    test_files = _read_dir(os.path.join(root, type_id, "test"))
    train_hashes = {hashlib.sha256(d).hexdigest() for _, d in train_files}
    leaks = [fp for fp, d in test_files if hashlib.sha256(d).hexdigest() in train_hashes]
    if leaks:
        raise ValueError(f"train/test overlap detected: {leaks[:3]} ...")
    return train_files, test_files


def _run(cmd, data):
    """Run a compressor command, feeding ``data`` on stdin, return stdout bytes."""
    return subprocess.run(
        cmd, input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True
    ).stdout


def _gzip_size(data):
    return len(_run(["gzip", "-9", "-c"], data))


def _zstd_size(data):
    return len(_run(["zstd", "-19", "-c"], data))


def _zstd_dict(train_files, workdir):
    """Train a zstd dictionary; return its path or None if training failed."""
    sample_paths = []
    for i, (_, data) in enumerate(train_files):
        p = os.path.join(workdir, f"s{i}.bin")
        with open(p, "wb") as fh:
            fh.write(data)
        sample_paths.append(p)
    dict_path = os.path.join(workdir, "zstd.dict")
    try:
        subprocess.run(
            ["zstd", "--train", *sample_paths, "-o", dict_path, "--maxdict=112640"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True,
        )
        return dict_path if os.path.exists(dict_path) else None
    except subprocess.CalledProcessError:
        return None


def _zstd_dict_size(data, dict_path):
    return len(_run(["zstd", "-19", "-D", dict_path, "-c"], data))


def run_benchmark(root, type_id, max_patterns=4096):
    train_files, test_files = load_split(root, type_id)
    if not train_files or not test_files:
        raise ValueError(f"need train and test files for '{type_id}' under {root}")

    model = train([d for _, d in train_files], type_id=type_id, max_patterns=max_patterns)
    model_size = len(model.save())

    totals = {"raw": 0, "ours": 0, "gzip": 0, "zstd": 0, "zstd_dict": 0}
    with tempfile.TemporaryDirectory() as workdir:
        dict_path = _zstd_dict(train_files, workdir)
        for _, data in test_files:
            ours = compress(data, model)
            assert decompress(ours, model) == data, "ROUND-TRIP FAILED — not lossless!"
            totals["raw"] += len(data)
            totals["ours"] += len(ours)
            totals["gzip"] += _gzip_size(data)
            totals["zstd"] += _zstd_size(data)
            totals["zstd_dict"] += _zstd_dict_size(data, dict_path) if dict_path else 0

    return {
        "type_id": type_id,
        "n_test": len(test_files),
        "model_size": model_size,
        "zstd_dict_available": dict_path is not None,
        "totals": totals,
    }


def format_report(report):
    t = report["totals"]
    raw = t["raw"] or 1
    lines = []
    lines.append(f"\n=== {report['type_id']}  ({report['n_test']} held-out files) ===")
    lines.append(f"model size (shipped once): {report['model_size']:,} bytes")
    lines.append(f"{'method':<14}{'bytes':>12}{'ratio':>10}{'vs raw':>10}")
    lines.append("-" * 46)

    def row(name, size):
        ratio = raw / size if size else 0.0
        pct = 100.0 * size / raw
        return f"{name:<14}{size:>12,}{ratio:>9.2f}x{pct:>9.1f}%"

    lines.append(row("raw", t["raw"]))
    lines.append(row("gzip -9", t["gzip"]))
    lines.append(row("zstd -19", t["zstd"]))
    if report["zstd_dict_available"]:
        lines.append(row("zstd -19 +dict", t["zstd_dict"]))
    else:
        lines.append("zstd -19 +dict   (unavailable — too few training samples)")
    lines.append(row("ours", t["ours"]))
    return "\n".join(lines)
