"""Batch-size sweep: does pertype's Mode-B advantage appear at small OTLP export batches?

The 512-span result showed pertype losing to gzip. That is the regime where LZ has ~163 KB of
in-batch context to exploit schema repetition. pertype's documented Mode-B edge is the opposite
regime: many SMALL independently-compressed payloads (<300 B) where per-payload overhead dominates
and a generic compressor starts cold every time. OTLP does not mandate a batch size, so small
batches are a real deployment (low-volume services, edge collectors, per-request export).
"""
import gzip, os, subprocess, sys, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import random
from otlp_benchmark import build_spans, otlp_batches, zstd_total

N = int(sys.argv[1]) if len(sys.argv) > 1 else 1200
rng = random.Random(20260805)
spans = build_spans(N, rng)
from pertype.model import train
from pertype.codec import compress as pt_compress

print(f"{'batch':>6}{'payloads':>10}{'B/payload':>11}{'gzip':>10}{'zstd-19':>10}{'pertype':>10}{'vs gzip':>10}")
print("-"*67)
for batch in (1, 4, 16, 64):
    pl = otlp_batches(spans, batch)
    ntr = max(1, len(pl)//3)
    tr, te = pl[:ntr], pl[ntr:]
    raw = sum(len(p) for p in te)
    gz = sum(len(gzip.compress(p, 9)) for p in te)
    with tempfile.TemporaryDirectory() as td:
        fs=[]
        for i,p in enumerate(te):
            fp=os.path.join(td,f"t{i:06d}.otlp"); open(fp,"wb").write(p); fs.append(fp)
        zs = zstd_total(fs)
    m = train(tr, type_id=f"otlp{batch}")
    pt = sum(len(pt_compress(p, m)) for p in te)
    print(f"{batch:>6}{len(te):>10}{raw/len(te):>11.0f}{gz:>10}{zs:>10}{pt:>10}"
          f"{100*(gz-pt)/gz:>9.1f}%")
