"""Small-batch OTLP with the FAIR bar: zstd --train (dictionary), not bare gzip.

pertype-trained gets a model built from held-out data. Comparing that against context-free gzip is
dictionary-vs-nothing and inflates our result. The honest competitor -- the same one the IoT target
used -- is `zstd --train`, which also gets a dictionary from the same training split.
"""
import gzip, os, subprocess, sys, tempfile, random
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from otlp_benchmark import build_spans, otlp_batches, zstd_total
from pertype.model import train
from pertype.codec import compress as pt_compress

N = int(sys.argv[1]) if len(sys.argv) > 1 else 1200
rng = random.Random(20260805)
spans = build_spans(N, rng)

print(f"{'batch':>6}{'test':>6}{'B/pl':>8}{'gzip':>10}{'zstd19':>10}{'zstd-dict':>11}{'pertype':>10}"
      f"{'vs gzip':>9}{'vs DICT':>9}")
print("-"*79)
for batch in (1, 4, 16, 64):
    pl = otlp_batches(spans, batch)
    ntr = max(1, len(pl)//3)
    tr, te = pl[:ntr], pl[ntr:]
    raw = sum(len(p) for p in te)
    gz = sum(len(gzip.compress(p, 9)) for p in te)
    with tempfile.TemporaryDirectory() as td:
        trd = os.path.join(td, "tr"); os.makedirs(trd)
        for i, p in enumerate(tr):
            open(os.path.join(trd, f"{i:06d}.otlp"), "wb").write(p)
        fs = []
        for i, p in enumerate(te):
            fp = os.path.join(td, f"t{i:06d}.otlp"); open(fp, "wb").write(p); fs.append(fp)
        zs = zstd_total(fs)
        dp = os.path.join(td, "d.dict")
        try:
            subprocess.run(["zstd", "--train", "-q", "-f", "--maxdict=65536", "-o", dp]
                           + [os.path.join(trd, f) for f in os.listdir(trd)],
                           check=True, capture_output=True)
            zd = zstd_total(fs, dp)
        except subprocess.CalledProcessError as e:
            zd = None
    m = train(tr, type_id=f"otlpf{batch}")
    pt = sum(len(pt_compress(p, m)) for p in te)
    zds = f"{zd:>11}" if zd else f"{'n/a':>11}"
    vd = f"{100*(zd-pt)/zd:>8.1f}%" if zd else f"{'--':>9}"
    print(f"{batch:>6}{len(te):>6}{raw/len(te):>8.0f}{gz:>10}{zs:>10}{zds}{pt:>10}"
          f"{100*(gz-pt)/gz:>8.1f}%{vd}")
