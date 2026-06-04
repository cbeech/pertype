"""Collect a REAL-WORLD corpus from this machine's filesystem.

Unlike scripts/make_corpus.py (synthetic, reproducible), this gathers genuine
files so the benchmark reflects real data:

  * json  — real .json files from /usr, /etc, $HOME (heterogeneous schemas)
  * html  — real .html files from system docs
  * logs  — real /var/log files split into many small line-chunks

Output goes to corpus_real/<type>/{train,test} (the layout the benchmark
expects). Train/test are split by content hash, so they are disjoint and the
selection is deterministic across runs on the same machine. The result is not
committed — it is local, machine-specific data.
"""
import hashlib
import os

ROOT = os.path.join(os.path.dirname(__file__), "..", "corpus_real")
HOME = os.path.expanduser("~")


def _walk_collect(dirs, exts, min_size, max_size, want, examine_cap=200_000):
    """Gather up to ``want`` distinct (by content) files matching exts/size."""
    seen, out = set(), []
    examined = 0
    for base in dirs:
        if not os.path.isdir(base):
            continue
        for root, _, files in os.walk(base):
            for fn in files:
                if not fn.lower().endswith(exts):
                    continue
                examined += 1
                path = os.path.join(root, fn)
                try:
                    size = os.path.getsize(path)
                    if size < min_size or size > max_size:
                        continue
                    with open(path, "rb") as fh:
                        data = fh.read()
                except OSError:
                    continue
                h = hashlib.sha256(data).hexdigest()
                if h in seen:
                    continue
                seen.add(h)
                out.append((h, data))
            if len(out) >= want or examined >= examine_cap:
                break
        if len(out) >= want or examined >= examine_cap:
            break
    out.sort()  # deterministic order by content hash
    return out[:want]


def _collect_logs(want, lines_per_chunk=40, min_size=200):
    """Split readable /var/log files into many small line-chunks."""
    seen, out = set(), []
    for root, _, files in os.walk("/var/log"):
        for fn in sorted(files):
            if not fn.endswith(".log"):
                continue
            path = os.path.join(root, fn)
            try:
                with open(path, "rb") as fh:
                    raw = fh.read()
            except OSError:
                continue
            lines = raw.split(b"\n")
            for i in range(0, len(lines), lines_per_chunk):
                chunk = b"\n".join(lines[i : i + lines_per_chunk]) + b"\n"
                if len(chunk) < min_size:
                    continue
                h = hashlib.sha256(chunk).hexdigest()
                if h in seen:
                    continue
                seen.add(h)
                out.append((h, chunk))
    out.sort()
    return out[:want]


def _write_split(type_id, files, train_frac=0.8):
    n_train = int(len(files) * train_frac)
    for split, items in (("train", files[:n_train]), ("test", files[n_train:])):
        d = os.path.join(ROOT, type_id, split)
        os.makedirs(d, exist_ok=True)
        for i, (h, data) in enumerate(items):
            with open(os.path.join(d, f"{i:04d}.{type_id}"), "wb") as fh:
                fh.write(data)
    print(f"{type_id}: {n_train} train + {len(files) - n_train} test "
          f"({sum(len(d) for _, d in files):,} bytes)")


def main():
    want = 375
    json_files = _walk_collect([HOME, "/etc", "/usr"], (".json",), 200, 32_768, want)
    html_files = _walk_collect(
        ["/usr/share/doc", "/usr/share", "/usr/lib"], (".html", ".htm"), 500, 65_536, want
    )
    log_files = _collect_logs(want)
    # source code: real Python modules (shared keywords/idioms/imports across files —
    # the trained-dictionary niche, like logs/html).
    code_files = _walk_collect(
        ["/usr/lib/python3", "/usr/lib/python3.13", "/usr/lib/python3/dist-packages"],
        (".py",), 300, 32_768, want,
    )
    for type_id, files in (("json", json_files), ("html", html_files),
                           ("logs", log_files), ("code", code_files)):
        if len(files) < 20:
            print(f"{type_id}: only {len(files)} files found — skipping")
            continue
        _write_split(type_id, files)


if __name__ == "__main__":
    main()
