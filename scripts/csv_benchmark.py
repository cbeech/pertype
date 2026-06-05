"""Delimited-text tables (CSV/TSV): our columnar codec vs gzip / xz / zstd.

Row-major CSV interleaves unlike values on every line; transposing to column-major and
coding each column with the strategy that fits it (fixed-decimal columns -> scaled-int
delta + ctxcoder, text columns -> deflate) beats the general tools on numeric tables.
`compressor.csvcolumnar` is round-trip verified at encode time (else it stores).

Usage: python3 scripts/csv_benchmark.py <file.csv> [max_rows]
e.g. the UCI household power consumption set (2M rows, ';'-delimited fixed-decimals).
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compressor import csvcolumnar


def sh(cmd, data):
    return len(subprocess.run(cmd, input=data, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, check=True).stdout)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    raw = open(sys.argv[1], "rb").read()
    if len(sys.argv) > 2:
        n = int(sys.argv[2])
        lines = raw.split(b"\n")
        raw = b"\n".join(lines[:n + 1]) + (b"\n" if raw.endswith(b"\n") else b"")

    blob = csvcolumnar.encode(raw)
    assert csvcolumnar.decode(blob) == raw, "round-trip FAILED"
    ours = len(blob)
    method = {0: "store", 1: "deflate", 2: "columnar"}[blob[4]]
    gz = sh(["gzip", "-9"], raw)
    xz = sh(["xz", "-9", "-c"], raw)
    zs = sh(["zstd", "-19", "-c"], raw)

    print(f"{os.path.basename(sys.argv[1])}  {len(raw)/1e6:.1f} MB")
    for name, sz in [("gzip -9", gz), ("zstd -19", zs), ("xz -9", xz),
                     (f"ours ({method})", ours)]:
        print(f"  {name:<18}{sz/1e6:>8.2f} MB   {len(raw)/sz:6.2f}x")
    best = min(gz, xz, zs)
    print(f"  -> ours is {'WIN' if ours < best else 'lose'} vs general "
          f"({(best-ours)/best*100:+.0f}% vs best general)")


if __name__ == "__main__":
    main()
