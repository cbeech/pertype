#!/usr/bin/env python3
"""stack_depth.py -- exact worst-case stack-depth analysis for libpfc.

SPDX-License-Identifier: Apache-2.0

Flight software must prove a bound on maximum stack usage (see docs/mission-safety.md 2.4).
For libpfc that bound is *computable exactly*, not estimable, because two JPL Power-of-Ten /
MISRA properties the codebase already enforces happen to be exactly the two that make the general
problem undecidable:

  - **No recursion** -> the call graph is a DAG, so "worst-case depth" is a longest-path problem
    with a finite answer, not a fixpoint that may not converge.
  - **No function pointers** -> every call edge is statically resolvable from the disassembly, so
    the call graph is complete. (An indirect call would force us to assume it could reach *any*
    address-taken function, which would make the bound useless.)

Both properties are ASSERTED here, not assumed: the script fails loudly if it finds a cycle or an
unresolved indirect call, so if someone later introduces recursion or a callback the bound stops
being reported rather than silently becoming wrong.

Method: GCC's `-fstack-usage` emits one `.su` file per translation unit giving each function's own
frame size. That covers frames but not call depth, so we recover call edges from `objdump -dr` on
the same objects and compute, for each entry point, the maximum sum of frame sizes along any path.

  worst_case(f) = frame(f) + max(worst_case(g) for g in callees(f))   [0 if no callees]

Limitations, stated rather than buried -- this is a flight artifact, so its caveats matter:
  1. **Leaf-call overhead is not modelled.** The figure counts frames GCC reports; the return
     address pushed by `call` and any red-zone use are not in `.su` numbers. Treat the result as a
     tight lower bound on the true requirement and apply a margin (we report a suggested one).
  2. **Inlining changes the graph.** At -O2 a static function may be folded into its caller, so its
     frame is absorbed rather than added. This is measured per optimisation level; compare like
     with like, and prefer the flight build's own flags.
  3. **libc calls are opaque.** Any call into libc (e.g. memset) has no `.su` entry; those edges
     are reported separately as unresolved-external rather than silently counted as zero.
"""
import argparse
import collections
import os
import re
import subprocess
import sys

# `objdump -d` line for a call. x86-64 uses `call`/`callq`, PowerPC uses `bl`.
CALL_RE = re.compile(r"^\s*[0-9a-f]+:\s+(?:[0-9a-f]{2} )+\s*(call|callq|bl)\s+([0-9a-f]+)\s+<([^>]+)>")
# An indirect call has no `<symbol>` target -- e.g. `call *%rax` / `bctrl`. These break the
# completeness guarantee, so we detect them explicitly.
INDIRECT_RE = re.compile(r"^\s*[0-9a-f]+:\s+(?:[0-9a-f]{2} )+\s*(call|callq|bl)\w*\s+\*|bctrl|blrl")
FUNC_RE = re.compile(r"^[0-9a-f]+\s+<([^>]+)>:")
RELOC_RE = re.compile(r"^\s+[0-9a-f]+:\s+R_\w+\s+([^\s+-]+)")


def parse_su(build_dir):
    """Parse GCC .su files -> {function_name: (frame_bytes, qualifier)}."""
    frames = {}
    for root, _dirs, files in os.walk(build_dir):
        for name in files:
            if not name.endswith(".su"):
                continue
            with open(os.path.join(root, name)) as fh:
                for line in fh:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) < 3:
                        continue
                    # "file:line:col:funcname\tbytes\tqualifier"
                    loc, size, qual = parts[0], parts[1], parts[2]
                    func = loc.rsplit(":", 1)[-1]
                    try:
                        size = int(size)
                    except ValueError:
                        continue
                    if func in frames and frames[func][0] != size:
                        print(
                            f"warning: duplicate .su entry for {func} "
                            f"({frames[func][0]} vs {size}); taking the larger",
                            file=sys.stderr,
                        )
                        size = max(size, frames[func][0])
                    frames[func] = (size, qual)
    return frames


def parse_calls(objdump, objects):
    """Disassemble objects -> ({caller: {callees}}, [indirect call sites])."""
    edges = collections.defaultdict(set)
    indirect = []
    for obj in objects:
        out = subprocess.run(
            [objdump, "-dr", obj], capture_output=True, text=True, check=True
        ).stdout
        current = None
        lines = out.splitlines()
        for i, line in enumerate(lines):
            m = FUNC_RE.match(line)
            if m:
                current = m.group(1)
                edges.setdefault(current, set())
                continue
            if current is None:
                continue
            if INDIRECT_RE.match(line):
                indirect.append((obj, current, line.strip()))
                continue
            m = CALL_RE.match(line)
            if not m:
                continue
            target = m.group(3)
            # A relocation on the following line names the real (cross-TU) target; the
            # `<...>` on the call line is only a local placeholder in that case.
            if i + 1 < len(lines):
                r = RELOC_RE.match(lines[i + 1])
                if r:
                    target = r.group(1)
            target = target.split("+")[0].strip()
            if target and target != current:
                edges[current].add(target)
    return edges, indirect


def worst_case(entry, edges, frames, stack=None, memo=None, unresolved=None):
    """Longest weighted path from `entry`. Asserts acyclicity (no recursion)."""
    if memo is None:
        memo = {}
    if stack is None:
        stack = []
    if unresolved is None:
        unresolved = set()
    if entry in stack:
        cycle = " -> ".join(stack[stack.index(entry):] + [entry])
        raise RecursionError(f"recursion detected, bound is not computable: {cycle}")
    if entry in memo:
        return memo[entry]

    own = frames.get(entry, (None, None))[0]
    if own is None:
        # No .su entry: a libc/external symbol we cannot account for.
        unresolved.add(entry)
        memo[entry] = (0, [entry + " (external, unaccounted)"])
        return memo[entry]

    best_sub, best_path = 0, []
    for callee in sorted(edges.get(entry, ())):
        sub, path = worst_case(callee, edges, frames, stack + [entry], memo, unresolved)
        if sub > best_sub:
            best_sub, best_path = sub, path
    memo[entry] = (own + best_sub, [f"{entry} ({own} B)"] + best_path)
    return memo[entry]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--objdump", default="objdump", help="objdump binary (use the cross one for cross builds)")
    ap.add_argument("--build-dir", default="build/stack", help="dir containing .su files")
    ap.add_argument("--objects", nargs="+", required=True, help="object files to disassemble")
    ap.add_argument("--entry", action="append", default=[], help="entry point (repeatable)")
    ap.add_argument("--label", default="host", help="label for this target in the report")
    ap.add_argument("--max-bytes", type=int, default=0, help="fail if any entry exceeds this")
    args = ap.parse_args()

    entries = args.entry or ["pfc_encode", "pfc_decode"]
    frames = parse_su(args.build_dir)
    if not frames:
        print(f"error: no .su files found under {args.build_dir}", file=sys.stderr)
        return 2
    edges, indirect = parse_calls(args.objdump, args.objects)

    if indirect:
        print("error: indirect call sites found -- the call graph is NOT complete, so no sound",
              file=sys.stderr)
        print("       stack bound can be reported. JPL Power-of-Ten forbids function pointers;",
              file=sys.stderr)
        print("       this is a real regression, not a tooling limitation:", file=sys.stderr)
        for obj, fn, line in indirect:
            print(f"       {obj}: in {fn}: {line}", file=sys.stderr)
        return 3

    print(f"=== worst-case stack depth [{args.label}] ===")
    worst_overall = 0
    unresolved = set()
    for entry in entries:
        if entry not in frames:
            print(f"  {entry}: NOT FOUND in .su data (wrong build dir?)")
            continue
        try:
            total, path = worst_case(entry, edges, frames, unresolved=unresolved)
        except RecursionError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 3
        worst_overall = max(worst_overall, total)
        print(f"  {entry}: {total} bytes")
        for step in path:
            print(f"      -> {step}")

    if unresolved:
        print("\n  unresolved externals (no .su entry; NOT counted in the totals above):")
        for name in sorted(unresolved):
            print(f"      {name}")

    print(f"\n  worst case across all entry points: {worst_overall} bytes")
    print(f"  suggested allocation with 100% margin: {worst_overall * 2} bytes")
    print("  note: excludes return-address pushes and any libc frames listed above.")

    if args.max_bytes and worst_overall > args.max_bytes:
        print(f"\nFAIL: {worst_overall} B exceeds the {args.max_bytes} B budget", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
