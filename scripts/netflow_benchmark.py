"""Measure-first: NetFlow / IPFIX flow records (real CTU-13 Argus bidirectional NetFlow) — Tier-2.

Premise: a flow record is a fixed schema (timestamp, src/dst IPv4, ports, proto, flags, ToS,
byte/packet counters) and the field stores it with field-blind LZ — nfdump packs binary nfcapd
records and compresses with LZO/LZ4/bzip2. That is the same "generic LZ, no per-field model" gap
the columnar de-interleave + per-column Δ/Δ² lever closed for financial ITCH (+49%) and CAN (+18%).

VERDICT (real data: CTU-13 / CTU-Malware-Capture-Botnet-50 `capture20110817.binetflow`, 300k real
bidirectional flows, packed into 54-byte records; round-trip verified):
- ❌ **RULED OUT.** The field-blind bar is hard to beat: on the packed binary, `xz -9` = **4.13×
  (3.92 MB)**, zstd-19 = 3.95×. Blanket pertype `columnar` (width-54) = **3.19× (5.08 MB) = −30%
  vs xz**. Even an **optimistic per-column ROUTED oracle** (best of zstd / value-Δ+zstd / columnar
  per field, with hindsight) only reaches **4.05 MB = −2.6% vs xz** (and just +7.8% vs zstd).
- **Why it differs from financial ITCH:** an ITCH record is *dominated* by sequential order IDs
  (Δ²→0) and monotonic ns timestamps — the columnar lever's ideal. A flow record is dominated by
  **non-sequential high-cardinality** fields: src/dst IPv4 (8 B), ports (8 B), byte/packet
  counters (20 B) = ~36 of 54 bytes where per-column Δ/Δ² *backfires* (differencing uncorrelated
  neighbours inflates entropy). Columnar de-interleave actively HURTS the IP columns (SrcAddr
  730 KB columnar vs 517 KB plain zstd; DstAddr 663 vs 310). Only StartTime + the counters suit
  Δ, and they are a minority of the record.
- **The redundancy LZ already eats:** the dominant structure is *cross-record repetition* of IP
  prefixes (147.32.84.x internal subnet) and labels — exactly what `xz` finds and what byte-plane
  de-interleaving destroys. The as-distributed text (CSV) even hits xz **8.3×** on its own verbose
  redundancy. There is no per-field lever the LZ bar lacks.

Same shape as the other negatives: the columnar Δ² win needs a record *dominated* by sequential /
monotonic integer fields; NetFlow's record is dominated by high-entropy categorical/counter fields.

Data: range-fetch an ~84 MB prefix of the public 286 MB CTU-13 binetflow (no login).
"""
import csv, datetime, gzip, lzma, socket, struct, subprocess, sys, time, urllib.request, zlib
import numpy as np
from pertype import columnar

URL = ("https://mcfp.felk.cvut.cz/publicDatasets/CTU-Malware-Capture-Botnet-50/"
       "detailed-bidirectional-flow-labels/capture20110817.binetflow")
PREFIX = 84_000_000
NROWS = int(sys.argv[1]) if len(sys.argv) > 1 else 300_000
REC = struct.Struct("<QIB4sIB4sIBBBIQQB")  # 54-byte flow record


def fetch():
    r = urllib.request.Request(URL, headers={"Range": f"bytes=0-{PREFIX - 1}"})
    return urllib.request.urlopen(r).read().decode("latin1")


def zstd(b, lvl=19):
    return subprocess.run(["zstd", f"-{lvl}", "-c"], input=b, stdout=subprocess.PIPE).stdout


def to_us(ts):
    d, t = ts.split(" "); Y, Mo, Da = d.split("/"); h, m, rest = t.split(":")
    s, us = rest.split(".")
    e = datetime.datetime(int(Y), int(Mo), int(Da), int(h), int(m), int(s))
    return int(e.timestamp()) * 1_000_000 + int(us)


def parse(text):
    dicts = {k: {} for k in ("Proto", "Dir", "State", "Label")}

    def idx(k, v):
        d = dicts[k]
        return d.setdefault(v, len(d))

    def ip4(s): return socket.inet_aton(s)
    def ip4i(s): return struct.unpack("<I", socket.inet_aton(s))[0]
    def port(s): return 0xFFFFFFFF if s == "" else int(s, 0) & 0xFFFFFFFF
    def tos(s): return 255 if s == "" else int(s) & 0xFF

    recs = bytearray()
    cols = {k: [] for k in ("t", "dur", "pro", "sa", "sp", "di", "da",
                             "dp", "st", "sT", "dT", "tp", "tb", "sb", "lb")}
    raw = []; skipped = 0
    r = csv.reader(text.splitlines()); next(r)
    for row in r:
        if len(row) != 15: continue
        try:
            t = to_us(row[0]); dur = min(int(round(float(row[1]) * 1e6)), 0xFFFFFFFF)
            pro = idx("Proto", row[2]); sp = port(row[4]); di = idx("Dir", row[5])
            dp = port(row[7]); st = idx("State", row[8]); sT = tos(row[9]); dT = tos(row[10])
            tp = int(row[11]) & 0xFFFFFFFF; tb = int(row[12]); sb = int(row[13])
            lb = idx("Label", row[14])
            recs += REC.pack(t, dur, pro, ip4(row[3]), sp, di, ip4(row[6]), dp, st,
                             sT, dT, tp, tb, sb, lb)
        except (OSError, ValueError):
            skipped += 1; continue
        for k, v in zip(cols, (t, dur, pro, ip4i(row[3]), sp, di, ip4i(row[6]),
                               dp, st, sT, dT, tp, tb, sb, lb)):
            cols[k].append(v)
        raw.append(",".join(row))
        if len(raw) >= NROWS: break
    return bytes(recs), cols, raw, dicts, skipped


def col_best(arr):
    b = np.ascontiguousarray(arr).tobytes()
    best = len(zstd(b))
    d = arr.copy(); d[1:] = arr[1:] - arr[:-1]
    best = min(best, len(zstd(np.ascontiguousarray(d).tobytes())))
    if arr.dtype.itemsize > 1:
        best = min(best, len(columnar.encode(b, width=arr.dtype.itemsize)))
    return best


def main():
    packed, cols, raw, dicts, skipped = parse(fetch())
    n = len(raw)
    csv_b = "\n".join(raw).encode()
    dict_blob = b"\x00".join(s.encode() for k in dicts for s in dicts[k])
    print(f"{n} flows, skipped {skipped} (non-IPv4); record {REC.size}B  "
          f"packed {len(packed)/1e6:.1f} MB  csv {len(csv_b)/1e6:.1f} MB\n", flush=True)

    print("== as-distributed (raw CSV text) ==", flush=True)
    for k, v in [("gzip", gzip.compress(csv_b, 9)), ("zstd", zstd(csv_b)),
                 ("xz", lzma.compress(csv_b, preset=9))]:
        print(f"   {k:5} {len(csv_b)/len(v):5.2f}x", flush=True)

    bz, bx = len(zstd(packed)), len(lzma.compress(packed, preset=9))
    col = columnar.encode(packed, width=REC.size)
    assert columnar.decode(col) == packed                       # lossless of the records
    blanket = len(col) + len(zlib.compress(dict_blob, 9))
    print(f"\n== packed binary flow records ({REC.size}B/rec); field-blind LZ = nfdump class ==",
          flush=True)
    for k, v in [("zstd-19 (field-blind)", bz), ("xz-9 (field-blind)", bx),
                 ("pertype columnar (blanket)", blanket)]:
        print(f"   {k:28} {len(packed)/v:5.2f}x  ({v/1e6:.2f} MB, "
              f"{(1-v/bx)*100:+5.1f}% vs xz)", flush=True)

    dt = {"t": np.uint64, "dur": np.uint32, "pro": np.uint8, "sa": np.uint32, "sp": np.uint32,
          "di": np.uint8, "da": np.uint32, "dp": np.uint32, "st": np.uint8, "sT": np.uint8,
          "dT": np.uint8, "tp": np.uint32, "tb": np.uint64, "sb": np.uint64, "lb": np.uint8}
    routed = sum(col_best(np.array(cols[k], dt[k])) for k in cols) + len(zlib.compress(dict_blob, 9))
    print(f"\n== per-column ROUTED oracle (best of zstd / Δ+zstd / columnar) ==", flush=True)
    print(f"   routed total {len(packed)/routed:5.2f}x  ({routed/1e6:.2f} MB)  "
          f"{(1-routed/bx)*100:+.1f}% vs xz, {(1-routed/bz)*100:+.1f}% vs zstd", flush=True)


if __name__ == "__main__":
    t = time.time(); main(); print(f"\n[{time.time()-t:.0f}s]")
