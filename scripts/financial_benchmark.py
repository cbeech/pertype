"""Measure-first: financial tick / order-book records (columnar + Δ/Δ²).

Real Binance aggTrades (BTCUSDT, one day, ~924k records) — the same fixed-layout tick
structure as NASDAQ ITCH / Databento DBN / LOBSTER: sequential trade IDs (Δ→1, so Δ²→0),
monotonic ms timestamps, tick-grid prices (fixed-point), low-cardinality boolean flags.

A real venue stores this as a compact fixed-width binary record stream, not CSV. We pack
the records into that form (little-endian integers; 8-byte fields scaled losslessly), then
compare our columnar Δ/Δ² codec (`pertype.columnar`) against zstd/gzip/xz on the SAME packed
bytes. Bar to beat: **zstd-generic at rest**. The columnar round-trip is verified byte-exact.

Data: FIN_CSV = Binance aggTrades CSV (no header; cols aggId,price,qty,firstId,lastId,
timeMs,isBuyerMaker,isBestMatch). FIN_MAXROWS caps rows (default 0 = all). Prices/qtys are
8-decimal fixed point on Binance, scaled to exact ints by ×1e8.
"""
import os
import subprocess
import time

import numpy as np

from pertype import columnar

CSV = os.environ.get("FIN_CSV", "data/fin/aggtrades.csv")
MAXROWS = int(os.environ.get("FIN_MAXROWS", "0"))

# 50-byte record: six 8-byte integer fields + two 1-byte flags.
# Columnar schema caps fields at 4 bytes, so each 8-byte field is two LE 4-byte columns
# (low word = the fast-varying / delta-friendly part; high word = near-constant).
SCHEMA = [4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 4, 1, 1]
RECW = sum(SCHEMA)  # 50


def ext(data, cmd):
    t = time.time()
    out = subprocess.run(cmd, input=data, stdout=subprocess.PIPE).stdout
    return len(out), time.time() - t


def scale8(strs):
    """8-decimal fixed-point strings -> exact int64 (×1e8). Binance always emits 8 dp."""
    i0, _, f0 = strs[0].partition(".")
    assert len(f0) == 8, f"expected 8 decimals, got {strs[0]!r}"
    return np.array([s.replace(".", "") for s in strs], dtype=np.uint64)


def main():
    t0 = time.time()
    with open(CSV) as fh:
        rows = fh.read().splitlines()
    if MAXROWS:
        rows = rows[:MAXROWS]
    cols = list(zip(*(r.split(",") for r in rows)))
    n = len(rows)

    aggId = np.array(cols[0], dtype=np.uint64)
    price = scale8(cols[1])
    qty = scale8(cols[2])
    firstId = np.array(cols[3], dtype=np.uint64)
    lastId = np.array(cols[4], dtype=np.uint64)
    timeMs = np.array(cols[5], dtype=np.uint64)
    bm = np.frombuffer(b"".join(b"\x01" if s == "True" else b"\x00" for s in cols[6]), np.uint8)
    bestm = np.frombuffer(b"".join(b"\x01" if s == "True" else b"\x00" for s in cols[7]), np.uint8)

    rec = np.zeros((n, RECW), np.uint8)

    def put8(a, off):
        rec[:, off:off + 8] = a.astype("<u8").view(np.uint8).reshape(n, 8)

    put8(aggId, 0); put8(price, 8); put8(qty, 16)
    put8(firstId, 24); put8(lastId, 32); put8(timeMs, 40)
    rec[:, 48] = bm; rec[:, 49] = bestm
    body = rec.tobytes()
    nb = len(body)
    print(f"records: {n:,}   packed: {nb / 1e6:.1f} MB ({RECW} B/rec)   "
          f"parse+pack {time.time() - t0:.1f}s\n")
    print(f"{'method':<26}{'size (MB)':>12}{'ratio':>9}{'B/rec':>9}{'time s':>9}")

    def row(name, size, secs):
        print(f"{name:<26}{size / 1e6:>12.3f}{nb / size:>9.2f}{size / n:>9.2f}{secs:>9.1f}")

    # context: generic on the raw CSV text
    csv_bytes = "\n".join(rows).encode()
    row("zstd -19  (raw CSV)", *ext(csv_bytes, ["zstd", "-19", "-c"]))

    # the real comparison — generic vs ours on the SAME packed binary (data at rest)
    row("gzip -9   (packed)", *ext(body, ["gzip", "-9"]))
    row("zstd -19  (packed, BAR)", *ext(body, ["zstd", "-19", "-c"]))
    row("xz -9     (packed)", *ext(body, ["xz", "-9", "-c"]))

    t = time.time()
    blob = columnar.encode(body, schema=SCHEMA)
    assert columnar.decode(blob) == body, "columnar round-trip FAILED"
    row("ours (columnar Δ/Δ²)", len(blob), time.time() - t)

    bar, _ = ext(body, ["zstd", "-19", "-c"])
    gain = (bar - len(blob)) / bar * 100
    print(f"\nours vs zstd-19 (packed): {bar / len(blob):.3f}x  "
          f"({gain:+.1f}% smaller)  -> {'WIN' if gain >= 3 else ('edge' if gain > 0 else 'lose')} "
          f"(bar = beat zstd-generic)")
    print("round-trip: OK (columnar decode == packed records, byte-exact)")


if __name__ == "__main__":
    main()
