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


# --- lossless video (.y4m, 4:2:0) -------------------------------------------

VY4M = b"VY4M"   # CLI container: y4m header line + a videocodec VYUV blob


def _read_y4m(path):
    """Parse a 4:2:0 .y4m into (header_line_bytes, Y, U, V) uint8 stacks."""
    import numpy as np
    raw = _read(path)
    nl = raw.index(b"\n")
    header = raw[:nl + 1]                       # first line, including the newline
    W = H = None
    for tok in raw[:nl].decode("ascii").split():
        if tok[0] == "W":
            W = int(tok[1:])
        elif tok[0] == "H":
            H = int(tok[1:])
        elif tok[0] == "C" and not tok.startswith("C420"):
            sys.exit(f"only 4:2:0 .y4m supported (header says {tok})")
    if W is None or H is None:
        sys.exit("malformed .y4m header")
    ys, cs = W * H, (W // 2) * (H // 2)
    fs = ys + 2 * cs
    pos, Ys, Us, Vs = nl + 1, [], [], []
    while pos < len(raw):
        pos = raw.index(b"\n", pos) + 1         # skip the per-frame "FRAME...\n"
        Ys.append(np.frombuffer(raw[pos:pos + ys], np.uint8).reshape(H, W))
        Us.append(np.frombuffer(raw[pos + ys:pos + ys + cs], np.uint8).reshape(H // 2, W // 2))
        Vs.append(np.frombuffer(raw[pos + ys + cs:pos + ys + 2 * cs], np.uint8).reshape(H // 2, W // 2))
        pos += fs
    return header, np.stack(Ys), np.stack(Us), np.stack(Vs)


def _write_y4m(path, header, Y, U, V):
    with open(path, "wb") as fh:
        fh.write(header)
        for t in range(len(Y)):
            fh.write(b"FRAME\n")
            fh.write(Y[t].tobytes()); fh.write(U[t].tobytes()); fh.write(V[t].tobytes())


def cmd_video_encode(args):
    from compressor import videocodec
    header, Y, U, V = _read_y4m(args.input)
    blob = VY4M + len(header).to_bytes(4, "big") + header + videocodec.encode_yuv(Y, U, V)
    dest = args.output or args.input + ".vid"
    _write(dest, blob)
    raw = Y.nbytes + U.nbytes + V.nbytes
    ratio = raw / len(blob) if blob else 0.0
    print(f"{args.input}: {len(Y)} frames {Y.shape[2]}x{Y.shape[1]} 4:2:0  "
          f"{raw:,} -> {len(blob):,} bytes ({ratio:.2f}x) -> {dest}")


def cmd_video_decode(args):
    from compressor import videocodec
    blob = _read(args.input)
    if blob[:4] != VY4M:
        sys.exit("not a VY4M video container")
    hlen = int.from_bytes(blob[4:8], "big")
    header = blob[8:8 + hlen]
    Y, U, V = videocodec.decode_yuv(blob[8 + hlen:])
    dest = args.output or (args.input[:-4] if args.input.endswith(".vid") else args.input + ".y4m")
    _write_y4m(dest, header, Y, U, V)
    print(f"{args.input}: -> {len(Y)} frames -> {dest}")


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

    ve = sub.add_parser("video-encode", help="losslessly encode a 4:2:0 .y4m video")
    ve.add_argument("input", help="input .y4m")
    ve.add_argument("-o", "--output", help="output .vid (default: <input>.vid)")
    ve.set_defaults(func=cmd_video_encode)

    vd = sub.add_parser("video-decode", help="decode a .vid back to .y4m")
    vd.add_argument("input", help="input .vid")
    vd.add_argument("-o", "--output", help="output .y4m (default: strips .vid)")
    vd.set_defaults(func=cmd_video_decode)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
