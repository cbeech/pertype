"""Command-line interface: train / compress / decompress / benchmark."""
import argparse
import os
import sys

from compressor.benchmark import format_report, run_benchmark
from compressor.codec import compress, decompress
from compressor.model import Model, train


def _read(path):
    with open(path, "rb") as fh:
        return fh.read()


def _write(path, data):
    with open(path, "wb") as fh:
        fh.write(data)


def cmd_train(args):
    files = []
    for name in sorted(os.listdir(args.corpus_dir)):
        fp = os.path.join(args.corpus_dir, name)
        if os.path.isfile(fp):
            files.append(_read(fp))
    if not files:
        sys.exit(f"no files found in {args.corpus_dir}")
    model = train(files, type_id=args.type_id, max_patterns=args.max_patterns)
    _write(args.output, model.save())
    print(
        f"trained '{args.type_id}' on {len(files)} files -> {args.output} "
        f"({len(model.dictionary.patterns)} patterns, {os.path.getsize(args.output):,} bytes)"
    )


def cmd_compress(args):
    model = Model.load(_read(args.model))
    data = _read(args.input)
    out = compress(data, model)
    dest = args.output or args.input + ".cz"
    _write(dest, out)
    ratio = len(data) / len(out) if out else 0.0
    print(f"{args.input}: {len(data):,} -> {len(out):,} bytes ({ratio:.2f}x) -> {dest}")


def cmd_decompress(args):
    model = Model.load(_read(args.model))
    data = decompress(_read(args.input), model)
    dest = args.output or (args.input[:-3] if args.input.endswith(".cz") else args.input + ".out")
    _write(dest, data)
    print(f"{args.input}: -> {len(data):,} bytes -> {dest}")


def cmd_benchmark(args):
    report = run_benchmark(args.root, args.type_id, max_patterns=args.max_patterns)
    print(format_report(report))


def build_parser():
    p = argparse.ArgumentParser(prog="compressor", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    t = sub.add_parser("train", help="train a model from a corpus directory")
    t.add_argument("type_id")
    t.add_argument("corpus_dir")
    t.add_argument("-o", "--output", required=True)
    t.add_argument("--max-patterns", type=int, default=4096, dest="max_patterns")
    t.set_defaults(func=cmd_train)

    c = sub.add_parser("compress", help="compress a file with a model")
    c.add_argument("input")
    c.add_argument("-m", "--model", required=True)
    c.add_argument("-o", "--output")
    c.set_defaults(func=cmd_compress)

    d = sub.add_parser("decompress", help="decompress a file with a model")
    d.add_argument("input")
    d.add_argument("-m", "--model", required=True)
    d.add_argument("-o", "--output")
    d.set_defaults(func=cmd_decompress)

    b = sub.add_parser("benchmark", help="benchmark vs gzip/zstd on held-out data")
    b.add_argument("type_id")
    b.add_argument("--root", default="corpus")
    b.add_argument("--max-patterns", type=int, default=4096, dest="max_patterns")
    b.set_defaults(func=cmd_benchmark)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
