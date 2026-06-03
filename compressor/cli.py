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

VY4M = b"VY4M"   # CLI container: y4m + per-frame headers + a videocodec VYUV blob


def _chroma_div(ctag):
    """Chroma subsampling (W-divisor, H-divisor) for a y4m C-tag, or None for
    monochrome. e.g. C420* -> (2, 2), C422 -> (2, 1), C444 -> (1, 1)."""
    body = ctag[1:]                              # drop leading 'C'
    if body.startswith("mono") or body.startswith("400"):
        return None
    for k, div in (("420", (2, 2)), ("411", (4, 1)), ("422", (2, 1)), ("444", (1, 1))):
        if body.startswith(k):
            return div
    sys.exit(f"unsupported y4m colour space {ctag}")


def _read_y4m(path):
    """Parse any .y4m into (header_line, [frame_headers], [planes]). Supports
    4:2:0 / 4:2:2 / 4:4:4 / mono and preserves each frame's header verbatim, so
    the file round-trips byte-exact. ``planes`` is [Y] (mono) or [Y, U, V]."""
    import numpy as np
    raw = _read(path)
    nl = raw.index(b"\n")
    header = raw[:nl + 1]
    W = H = None
    ctag = "C420"
    for tok in raw[:nl].decode("ascii").split():
        if tok[0] == "W":
            W = int(tok[1:])
        elif tok[0] == "H":
            H = int(tok[1:])
        elif tok[0] == "C":
            ctag = tok
    if W is None or H is None:
        sys.exit("malformed .y4m header")
    div = _chroma_div(ctag)
    ys = W * H
    cw, ch = (W // div[0], H // div[1]) if div else (0, 0)
    cs = cw * ch
    fheaders, Ys, Us, Vs = [], [], [], []
    pos = nl + 1
    while pos < len(raw):
        fnl = raw.index(b"\n", pos)              # this frame's "FRAME...\n", verbatim
        fheaders.append(raw[pos:fnl + 1])
        pos = fnl + 1
        Ys.append(np.frombuffer(raw[pos:pos + ys], np.uint8).reshape(H, W)); pos += ys
        if div:
            Us.append(np.frombuffer(raw[pos:pos + cs], np.uint8).reshape(ch, cw)); pos += cs
            Vs.append(np.frombuffer(raw[pos:pos + cs], np.uint8).reshape(ch, cw)); pos += cs
    planes = [np.stack(Ys)] + ([np.stack(Us), np.stack(Vs)] if div else [])
    return header, fheaders, planes


def _write_y4m(path, header, fheaders, planes):
    with open(path, "wb") as fh:
        fh.write(header)
        for t in range(len(planes[0])):
            fh.write(fheaders[t])
            for p in planes:
                fh.write(p[t].tobytes())


def cmd_video_encode(args):
    from compressor import videocodec
    header, fheaders, planes = _read_y4m(args.input)
    fhblob = b"".join(fheaders)
    blob = (VY4M + bytes([len(planes)])
            + len(header).to_bytes(4, "big") + header
            + len(fhblob).to_bytes(4, "big") + fhblob
            + videocodec.encode_yuv(*planes))
    dest = args.output or args.input + ".vid"
    _write(dest, blob)
    raw = sum(p.nbytes for p in planes)
    ratio = raw / len(blob) if blob else 0.0
    Y = planes[0]
    print(f"{args.input}: {len(Y)} frames {Y.shape[2]}x{Y.shape[1]} "
          f"{len(planes)}-plane  {raw:,} -> {len(blob):,} bytes ({ratio:.2f}x) -> {dest}")


def cmd_video_decode(args):
    from compressor import videocodec
    blob = _read(args.input)
    if blob[:4] != VY4M:
        sys.exit("not a VY4M video container")
    pos = 5                                       # skip magic + n_planes byte
    hlen = int.from_bytes(blob[pos:pos + 4], "big"); pos += 4
    header = blob[pos:pos + hlen]; pos += hlen
    fhlen = int.from_bytes(blob[pos:pos + 4], "big"); pos += 4
    fhblob = blob[pos:pos + fhlen]; pos += fhlen
    fheaders = [line + b"\n" for line in fhblob.split(b"\n")[:-1]]
    planes = videocodec.decode_yuv(blob[pos:])
    dest = args.output or (args.input[:-4] if args.input.endswith(".vid") else args.input + ".y4m")
    _write_y4m(dest, header, fheaders, planes)
    print(f"{args.input}: -> {len(planes[0])} frames -> {dest}")


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

    ve = sub.add_parser("video-encode",
                        help="losslessly encode a .y4m video (4:2:0/4:2:2/4:4:4/mono)")
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
