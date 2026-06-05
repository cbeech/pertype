"""Public text benchmark: enwik8 (Wikipedia), our *amortized* design vs the standard tools.

enwik8 is the canonical Large Text Compression Benchmark corpus (first 100 MB of a
Wikipedia XML dump). Our codec is per-type *trained*: the model ships once and is
amortized across many files of a type, so the honest test is **held-out**, not
self-contained single-file (where the dictionary-as-model overhead dominates — by
design). We mirror "many Wikipedia-text files" by slicing enwik8 into fixed blocks,
training on one disjoint set and compressing another, and report bits-per-character
(the LTCB metric) vs gzip / bzip2 / xz / zstd / zstd --train on the *same held-out*
blocks. The model is built from the train blocks only and reported separately (it is
paid once); every block's round-trip is verified before its size is counted.

Usage: python3 scripts/enwik_benchmark.py [enwik8_path] [block_kb] [n_train] [n_test]
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compressor.benchmark import _zstd_dicts, _zstd_dict_size
from compressor.codec import compress, decompress
from compressor.model import train

PATH = sys.argv[1] if len(sys.argv) > 1 else "/home/user/sci_data/enwik/enwik8"
BLK = (int(sys.argv[2]) if len(sys.argv) > 2 else 64) * 1024
N_TRAIN = int(sys.argv[3]) if len(sys.argv) > 3 else 32
N_TEST = int(sys.argv[4]) if len(sys.argv) > 4 else 16


def _run(cmd, data):
    return len(subprocess.run(cmd, input=data, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, check=True).stdout)


def main():
    raw = open(PATH, "rb").read()
    blocks = [raw[i * BLK:(i + 1) * BLK] for i in range(N_TRAIN + N_TEST)]
    train_blocks = blocks[:N_TRAIN]
    test_blocks = blocks[N_TRAIN:N_TRAIN + N_TEST]
    test_bytes = sum(len(b) for b in test_blocks)
    print(f"enwik8: {BLK // 1024} KB blocks, train on {N_TRAIN} ({N_TRAIN * BLK // 1024} KB), "
          f"held-out test on {N_TEST} ({test_bytes:,} B)")

    print("training model (slow — pure-Python pattern mining)...", flush=True)
    model = train(list(train_blocks), type_id="enwik")
    model_size = len(model.save())

    tot = {"ours": 0, "gzip": 0, "bzip2": 0, "xz": 0, "zstd": 0, "zstd_dict": 0}
    with tempfile.TemporaryDirectory() as wd:
        dict_paths = _zstd_dicts([(None, b) for b in train_blocks], wd)
        dt = {dp: sum(_zstd_dict_size(b, dp) for b in test_blocks) for dp in dict_paths}
        best_dict = min(dt, key=dt.get) if dt else None
        for b in test_blocks:
            c = compress(b, model)
            assert decompress(c, model) == b, "ROUND-TRIP FAILED — not lossless!"
            tot["ours"] += len(c)
            tot["gzip"] += _run(["gzip", "-9", "-c"], b)
            tot["bzip2"] += _run(["bzip2", "-9", "-c"], b)
            tot["xz"] += _run(["xz", "-9", "-c"], b)
            tot["zstd"] += _run(["zstd", "-19", "-c"], b)
        tot["zstd_dict"] = dt[best_dict] if best_dict else 0

    print(f"\nmodel (shipped once, amortized): {model_size:,} B")
    print(f"{'method':<16}{'bytes':>12}{'ratio':>9}{'bits/char':>11}")
    print("-" * 48)
    rows = [("gzip -9", tot["gzip"]), ("bzip2 -9", tot["bzip2"]), ("xz -9", tot["xz"]),
            ("zstd -19", tot["zstd"])]
    if best_dict:
        md = int(os.path.basename(best_dict).split("_")[1].split(".")[0])
        rows.append((f"zstd --train@{md // 1024}K", tot["zstd_dict"]))
    rows.append(("ours (held-out)", tot["ours"]))
    for name, sz in rows:
        print(f"{name:<16}{sz:>12,}{test_bytes / sz:>8.2f}x{8 * sz / test_bytes:>10.3f}")


if __name__ == "__main__":
    main()
