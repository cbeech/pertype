"""Measure-first: automotive CAN-bus / MDF4 logs (target #12).

A CAN frame stream is a fixed-layout record table: a monotonic timestamp (regular bus cadence →
Δ²≈0), a small set of arbitration IDs (low cardinality), a DLC, and up to 8 data bytes (each a
slowly-varying signal column). MDF4 stores this with only per-block DEFLATE — no field awareness.
De-interleaving into per-field columns + per-column Δ/Δ² (our `pertype.columnar`, the same codec
that won on financial tick) should beat it. Bar: gzip (≈ MDF4 per-block deflate) and zstd.

NOTE: validated only **directionally** — the readily-available real log (python-can
`issue_1256.asc`, 1457 frames ≈ 22 KB packed) is small. The columnar mechanism is identical to
the financial win, so the +18% directional result is plausible; a multi-MB real log (CANedge /
Car-Hacking HCRL) would solidify it. Set CAN_ASC to a Vector ASC log.
  curl -O https://raw.githubusercontent.com/hardbyte/python-can/main/test/data/issue_1256.asc
Set CAN_LOG to a candump-style log (the UWindsor CarHackingResearch NissanJuke capture:
`  can0  160   [7]  31 23 26 00 08 FF E8`, or `(ts) can0 160#3123260008FFE8` with timestamps),
or CAN_HCRL to an HCRL Car-Hacking txt (`Timestamp: 1479121434.850202  ID: 0350  000  DLC: 8  05 28 ...`).
Timestamp-less captures exercise the ID/data columns only (no ts-Δ² lever). Set CAN_SORT=1 to
stable-sort frames by ID before packing — time-ordered mixed-ID streams defeat per-column Δ,
and grouping by ID is what field tools (MDF) assume.
"""
import os
import re
import subprocess

import numpy as np

from pertype import columnar

ASC = os.environ.get("CAN_ASC", "data/can/issue_1256.asc")
LOG = os.environ.get("CAN_LOG")
HCRL = os.environ.get("CAN_HCRL")
SORT = bool(os.environ.get("CAN_SORT"))
LINE = re.compile(r"\s*([0-9.]+)\s+\d+\s+([0-9A-Fa-f]+)\s+\w+\s+d\s+(\d+)\s+(.*?)\s+Length")
DUMP = re.compile(r"\s*(?:\(([0-9.]+)\)\s+)?\S+\s+([0-9A-Fa-f]+)\s+(?:\[(\d+)\]\s+(.*?)\s*$|#([0-9A-Fa-f]*))")
HCRLL = re.compile(r"Timestamp:\s+([0-9.]+)\s+ID:\s+([0-9A-Fa-f]+)\s+\S+\s+DLC:\s+(\d+)\s*(.*)")
SCHEMA_TS = [4, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1]    # ts(4) id(2) dlc(1) data[8]; sums to 15
SCHEMA_NOTS = [2, 1, 1, 1, 1, 1, 1, 1, 1, 1]     # id(2) dlc(1) data[8]; sums to 11


def parse():
    rows, path = [], HCRL or LOG or ASC
    for ln in open(path, errors="replace"):
        if HCRL:
            m = HCRLL.match(ln)
            if not m:
                continue
            data = [int(x, 16) for x in m.group(4).split()][:8]
            rows.append((float(m.group(1)), int(m.group(2), 16), int(m.group(3)), data))
        elif LOG:
            m = DUMP.match(ln)
            if not m:
                continue
            ts = float(m.group(1)) if m.group(1) else None
            data = [int(m.group(4)[i:i + 2], 16) for i in range(0, len(m.group(4) or ""), 2)] \
                if m.group(4) is not None else [int(m.group(5)[i:i + 2], 16)
                                                for i in range(0, len(m.group(5)), 2)]
            rows.append((ts, int(m.group(2), 16), int(m.group(3) or len(data)), data))
        else:
            m = LINE.match(ln)
            if not m:
                continue
            data = [int(x, 16) for x in m.group(4).split()][:8]
            rows.append((float(m.group(1)), int(m.group(2), 16), int(m.group(3)), data))
    return rows


def main():
    rows = parse()
    n = len(rows)
    has_ts = rows and rows[0][0] is not None
    if SORT:
        rows.sort(key=lambda r: r[1])  # stable: group by ID, keep arrival order within
    width = 15 if has_ts else 11
    rec = np.zeros((n, width), np.uint8)
    if has_ts:
        t0 = rows[0][0]
        tus = np.round((np.array([r[0] for r in rows]) - t0) * 1e6).astype(np.uint64)
        rec[:, 0:4] = tus.astype("<u8").view(np.uint8).reshape(n, 8)[:, :4]
    off = 4 if has_ts else 0
    rec[:, off:off + 2] = np.array([r[1] for r in rows], np.uint16).view(np.uint8).reshape(n, 2)
    rec[:, off + 2] = [r[2] for r in rows]
    for i, r in enumerate(rows):
        rec[i, off + 3:off + 11] = r[3][:8] + [0] * (8 - len(r[3]))
    body = rec.tobytes(); N = len(body)

    def sh(cmd):
        return len(subprocess.run(cmd, input=body, stdout=subprocess.PIPE).stdout)

    gz = sh(["gzip", "-9"]); zs = sh(["zstd", "-19", "-c"]); xz = sh(["xz", "-9", "-c"])
    blob = columnar.encode(body, schema=SCHEMA_TS if has_ts else SCHEMA_NOTS)
    assert columnar.decode(blob) == body, "columnar round-trip FAILED"
    print(f"CAN frames: {n:,}   packed {N/1e3:.1f} KB ({width} B/rec, "
          f"timestamps {'yes' if has_ts else 'NO'}, ID-sorted {'yes' if SORT else 'no'})\n")
    print(f"  {'gzip -9 (≈MDF4 deflate, BAR)':<30}{N/gz:>6.2f}x")
    print(f"  {'zstd -19':<30}{N/zs:>6.2f}x")
    print(f"  {'xz -9':<30}{N/xz:>6.2f}x")
    print(f"  {'ours (columnar Δ/Δ²)':<30}{N/len(blob):>6.2f}x   "
          f"-> {(gz-len(blob))/gz*100:+.0f}% vs gzip/MDF4   round-trip OK")


if __name__ == "__main__":
    main()
