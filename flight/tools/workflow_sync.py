#!/usr/bin/env python3
"""workflow_sync.py -- verify the GitHub and Gitea CI workflows have identical STRUCTURE.

SPDX-License-Identifier: Apache-2.0

The two files are deliberately near-identical and kept in sync BY HAND, which is exactly the
arrangement that produces silent drift: a gate gets added, tightened or fixed in one copy and the
other quietly keeps testing the old thing. That failure is invisible -- both files stay valid YAML
and both keep passing -- until the runner you actually push to turns out to be running a weaker
suite than you think.

A plain `diff` cannot police this, because the files are SUPPOSED to differ: each carries its own
header explaining its purpose and uncertainty, and each filters on its own path. So compare what
must match -- job names, their `container:`, and the ordered list of step names and run bodies --
and ignore prose entirely.

Exit 0 if the structures match, 1 with a report if they diverge.
"""
import os
import re
import sys


def strip_comments(text):
    """Drop whole-line comments. Trailing comments are left alone: a `#` can legitimately appear
    inside a run: body (shell comments, `sha256sum -c -` lines, URLs with fragments)."""
    return "\n".join(l for l in text.split("\n") if not l.lstrip().startswith("#"))


def parse(path):
    """Minimal structural parse. Deliberately not PyYAML: these workflows must be checkable on a
    bare CI image with nothing installed beyond python3, which is the same constraint the rest of
    tools/ already works under."""
    text = strip_comments(open(path, encoding="utf-8").read())
    jobs = {}
    cur = None
    for line in text.split("\n"):
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m and not line.startswith("    "):
            cur = m.group(1)
            jobs[cur] = {"container": None, "steps": []}
            continue
        if cur is None:
            continue
        m = re.match(r"^    container:\s*(\S+)", line)
        if m:
            jobs[cur]["container"] = m.group(1)
        m = re.match(r"^      - name:\s*(.+?)\s*$", line)
        if m:
            jobs[cur]["steps"].append(("name", m.group(1)))
        m = re.match(r"^      - uses:\s*(\S+)", line)
        if m:
            jobs[cur]["steps"].append(("uses", m.group(1)))
        m = re.match(r"^        run:\s*(.+?)\s*$", line)
        if m:
            jobs[cur]["steps"].append(("run", m.group(1)))
    # `on:` triggers land in the same 2-space namespace as jobs; drop the known non-jobs.
    for k in ("push", "pull_request", "workflow_dispatch"):
        jobs.pop(k, None)
    return jobs


def main():
    # Resolve from this file's location (flight/tools/ -> flight/ -> repo root) so the check works
    # from any cwd -- `make` runs it from flight/, CI may run it from the root.
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    a_path = os.path.join(root, ".github", "workflows", "flight-ci.yml")
    b_path = os.path.join(root, ".gitea", "workflows", "flight-ci.yml")
    for pth in (a_path, b_path):
        if not os.path.exists(pth):
            print(f"error: {pth} not found", file=sys.stderr)
            return 2
    a, b = parse(a_path), parse(b_path)
    problems = []

    only_a, only_b = sorted(set(a) - set(b)), sorted(set(b) - set(a))
    if only_a:
        problems.append(f"jobs only in .github: {', '.join(only_a)}")
    if only_b:
        problems.append(f"jobs only in .gitea: {', '.join(only_b)}")

    for job in sorted(set(a) & set(b)):
        if a[job]["container"] != b[job]["container"]:
            problems.append(f"{job}: container differs -- "
                            f"github={a[job]['container']} gitea={b[job]['container']}")
        if a[job]["steps"] != b[job]["steps"]:
            sa, sb = a[job]["steps"], b[job]["steps"]
            problems.append(f"{job}: step structure differs ({len(sa)} vs {len(sb)} entries)")
            for i in range(max(len(sa), len(sb))):
                x = sa[i] if i < len(sa) else None
                y = sb[i] if i < len(sb) else None
                if x != y:
                    problems.append(f"    step {i}: github={x!r}")
                    problems.append(f"             gitea={y!r}")

    if problems:
        print("CI workflow drift detected -- the two copies must stay structurally identical:")
        for p in problems:
            print(f"  {p}")
        return 1
    print(f"workflow sync OK: {len(a)} jobs, identical structure "
          f"({', '.join(sorted(a))})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
