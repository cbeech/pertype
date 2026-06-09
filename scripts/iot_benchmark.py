"""Measure-first: MQTT/IoT telemetry — the Mode-B (trained-dictionary) opportunity.

Realistic IoT publishes are MANY SMALL same-schema messages, each compressed independently
(you can't batch — every publish is its own packet). That's exactly where a per-type trained
model should beat generic per-message gzip/zstd AND zstd's own trained dictionary (`zstd
--train`) — the headline claim. We test it on the public Intel Lab sensor dataset, reshaped
into per-reading JSON messages.

Data: http://db.csail.mit.edu/labdata/data.txt.gz  (set IOT_DATA to the decompressed .txt).
Fields per line: date time epoch moteid temperature humidity light voltage.

Usage: IOT_DATA=/tmp/iot.txt PYTHONPATH=. python3 scripts/iot_benchmark.py [n_train] [n_test]
"""
import gzip
import json
import lzma
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pertype.model import train
from pertype.codec import compress as pt_compress

DATA = os.environ.get("IOT_DATA", "/tmp/iot.txt")
N_TRAIN = int(sys.argv[1]) if len(sys.argv) > 1 else 1500
N_TEST = int(sys.argv[2]) if len(sys.argv) > 2 else 600


def messages(path, want):
    """Yield up to `want` realistic JSON telemetry messages from valid readings."""
    out = []
    with open(path) as fh:
        for line in fh:
            p = line.split()
            if len(p) != 8:
                continue  # skip malformed (missing fields)
            # preserve the source numeric strings (deterministic, lossless transform)
            msg = ('{"ts":"%s %s","mote":%s,"temp":%s,"hum":%s,"light":%s,"volt":%s}\n'
                   % (p[0], p[1], p[3], p[4], p[5], p[6], p[7]))
            out.append(msg.encode())
            if len(out) >= want:
                break
    return out


def zstd_total(test_files, dict_path=None):
    """Compress each file at -19 (optionally with a trained dict); return total .zst bytes."""
    with tempfile.TemporaryDirectory() as od:
        cmd = ["zstd", "-19", "-q", "-f", "--output-dir-flat", od]
        if dict_path:
            cmd += ["-D", dict_path]
        subprocess.run(cmd + test_files, check=True)
        return sum(os.path.getsize(os.path.join(od, f + ".zst"))
                   for f in (os.path.basename(x) for x in test_files))


def main():
    if not os.path.exists(DATA):
        sys.exit(f"no data at {DATA} — download db.csail.mit.edu/labdata/data.txt.gz and set IOT_DATA")
    msgs = messages(DATA, N_TRAIN + N_TEST)
    train_msgs, test_msgs = msgs[:N_TRAIN], msgs[N_TRAIN:N_TRAIN + N_TEST]
    raw = sum(len(m) for m in test_msgs)
    print(f"Intel Lab IoT: {len(train_msgs)} train + {len(test_msgs)} test messages, "
          f"avg {raw / len(test_msgs):.0f} B/msg, {raw} raw test bytes\n")

    with tempfile.TemporaryDirectory() as td:
        # write the test set as individual files (the many-small-files scenario) for zstd/gzip
        traindir = os.path.join(td, "train"); os.makedirs(traindir)
        for i, m in enumerate(train_msgs):
            open(os.path.join(traindir, f"{i:06d}.json"), "wb").write(m)
        testfiles = []
        for i, m in enumerate(test_msgs):
            fp = os.path.join(td, f"t{i:06d}.json"); open(fp, "wb").write(m); testfiles.append(fp)

        results = []
        # raw
        results.append(("raw (uncompressed)", raw, None))
        # gzip -9 per message
        results.append(("gzip -9 (per msg)", sum(len(gzip.compress(m, 9)) for m in test_msgs), None))
        # xz -9 per message
        results.append(("xz -9 (per msg)", sum(len(lzma.compress(m, preset=9)) for m in test_msgs), None))
        # zstd -19 per message, no dictionary
        results.append(("zstd -19 (per msg)", zstd_total(testfiles), None))
        # zstd --train dictionary, then zstd -19 -D per message  (THE competitor)
        dict_path = os.path.join(td, "iot.dict")
        subprocess.run(["zstd", "--train", "-q", "-f", "--maxdict=65536",
                        "-o", dict_path] + [os.path.join(traindir, f) for f in os.listdir(traindir)],
                       check=True)
        results.append(("zstd --train (dict)", zstd_total(testfiles, dict_path), "BAR"))
        # pertype: train a per-type model, compress each test message
        t = time.perf_counter()
        model = train(train_msgs, type_id="iot")
        tt = time.perf_counter() - t
        pt = sum(len(pt_compress(m, model)) for m in test_msgs)
        results.append((f"pertype (trained, use_lz={model.use_lz})", pt, "OURS"))

    print(f"{'method':<34}{'bytes':>10}{'ratio':>8}{'B/msg':>8}")
    print("-" * 60)
    bar = next(b for n, b, tag in results if tag == "BAR")
    for name, b, tag in results:
        mark = ""
        if tag == "OURS":
            mark = f"   <- {'BEATS' if b < bar else 'loses to'} zstd --train by {100*(bar-b)/bar:+.1f}%"
        print(f"{name:<34}{b:>10}{raw/b:>7.2f}x{b/len(test_msgs):>8.1f}{mark}")
    print(f"\n(per-message compression — the honest IoT metric; pertype train took {tt:.0f}s)")


if __name__ == "__main__":
    main()
