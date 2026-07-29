#!/usr/bin/env python3
"""mcdc_gate.py -- parse `llvm-cov export` JSON and enforce an MC/DC coverage floor.

SPDX-License-Identifier: Apache-2.0

MC/DC (modified condition/decision coverage) is the structural-coverage metric DO-178C requires
for the highest criticality levels, and it is strictly stronger than the branch coverage `gcovr`
already gates in CI: a compound decision like `(a && b) || c` can reach both outcomes -- 100%
branch -- while never demonstrating that `b` alone can change the result. gcov cannot measure it at
all; this needs clang 18+'s `-fcoverage-mcdc`.

Why a separate gate rather than folding into `make coverage`: the two use different toolchains
(gcov/gcovr vs clang/llvm-cov) and, more importantly, sit at very different maturity levels. Line
and branch coverage are near their ceiling and gate close to it. MC/DC starts far lower, so its
floor is a RATCHET -- set just under the current measurement, raised as conditions get covered --
not a claim of compliance. Reporting them separately keeps that distinction visible instead of
letting a strong branch number imply a strong MC/DC one.
"""
import argparse
import json
import sys


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export_json", help="output of `llvm-cov export ... --format=text`")
    ap.add_argument("--min-percent", type=float, required=True,
                    help="fail below this MC/DC percentage (a ratchet, not a compliance target)")
    ap.add_argument("--exclude", default="test/",
                    help="skip files whose path contains this substring")
    args = ap.parse_args()

    with open(args.export_json) as fh:
        data = json.load(fh)

    rows = []
    tot_covered = tot_count = 0
    for export in data.get("data", []):
        for f in export.get("files", []):
            name = f.get("filename", "")
            if args.exclude and args.exclude in name:
                continue
            s = f.get("summary", {}).get("mcdc", {})
            count, covered = s.get("count", 0), s.get("covered", 0)
            if count == 0:
                continue
            rows.append((name, covered, count, 100.0 * covered / count))
            tot_covered += covered
            tot_count += count

    if tot_count == 0:
        print("error: no MC/DC data found -- was the build compiled with -fcoverage-mcdc?",
              file=sys.stderr)
        return 2

    rows.sort(key=lambda r: r[3])
    print("MC/DC coverage by file (worst first):")
    for name, cov, cnt, pct in rows:
        short = name.split("/src/")[-1] if "/src/" in name else name.rsplit("/", 1)[-1]
        print(f"  {short:24s} {cov:4d}/{cnt:<4d} {pct:6.2f}%")

    pct = 100.0 * tot_covered / tot_count
    print(f"\nTOTAL MC/DC: {tot_covered}/{tot_count} conditions = {pct:.2f}%")
    print(f"floor: {args.min_percent:.2f}%")

    if pct < args.min_percent:
        print(f"\nFAIL: MC/DC {pct:.2f}% is below the {args.min_percent:.2f}% floor",
              file=sys.stderr)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
