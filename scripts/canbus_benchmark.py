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
"""
import os
import re
import subprocess

import numpy as np

from pertype import columnar

ASC = os.environ.get("CAN_ASC", "data/can/issue_1256.asc")
LINE = re.compile(r"\s*([0-9.]+)\s+\d+\s+([0-9A-Fa-f]+)\s+\w+\s+d\s+(\d+)\s+(.*?)\s+Length")
SCHEMA = [4, 2, 1, 1, 1, 1, 1, 1, 1, 1, 1]  # ts(4) id(2) dlc(1) data[8]; sums to 15


def main():
    rows = []
    for ln in open(ASC):
        m = LINE.match(ln)
        if not m:
            continue
        data = [int(x, 16) for x in m.group(4).split()][:8]
        data += [0] * (8 - len(data))
        rows.append((float(m.group(1)), int(m.group(2), 16), int(m.group(3)), data))
    n = len(rows)
    rec = np.zeros((n, 15), np.uint8)
    t0 = rows[0][0]
    tus = np.round((np.array([r[0] for r in rows]) - t0) * 1e6).astype(np.uint64)
    rec[:, 0:4] = tus.astype("<u8").view(np.uint8).reshape(n, 8)[:, :4]
    rec[:, 4:6] = np.array([r[1] for r in rows], np.uint16).view(np.uint8).reshape(n, 2)
    rec[:, 6] = [r[2] for r in rows]
    for i, r in enumerate(rows):
        rec[i, 7:15] = r[3]
    body = rec.tobytes(); N = len(body)

    def sh(cmd):
        return len(subprocess.run(cmd, input=body, stdout=subprocess.PIPE).stdout)

    gz = sh(["gzip", "-9"]); zs = sh(["zstd", "-19", "-c"]); xz = sh(["xz", "-9", "-c"])
    blob = columnar.encode(body, schema=SCHEMA)
    assert columnar.decode(blob) == body, "columnar round-trip FAILED"
    print(f"CAN frames: {n:,}   packed {N/1e3:.1f} KB (15 B/rec)\n")
    print(f"  {'gzip -9 (≈MDF4 deflate, BAR)':<30}{N/gz:>6.2f}x")
    print(f"  {'zstd -19':<30}{N/zs:>6.2f}x")
    print(f"  {'xz -9':<30}{N/xz:>6.2f}x")
    print(f"  {'ours (columnar Δ/Δ²)':<30}{N/len(blob):>6.2f}x   "
          f"-> {(gz-len(blob))/gz*100:+.0f}% vs gzip/MDF4   round-trip OK")


if __name__ == "__main__":
    main()
