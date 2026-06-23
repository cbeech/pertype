"""Measure-first: smart-meter / AMI load profiles (real UCI household power, 1-min, 7 channels) — Tier-2.

Premise: a load profile is a slowly-varying per-meter time-series with strong daily/weekly
periodicity — seemingly the smooth-time-series shape that won for EEG/mocap. The hope was that
pertype's existing per-channel predictor + `ctxcoder` would beat the as-shipped CSV and the named
per-meter specialists (DEGA = differential exp-Golomb + arithmetic; LZMA).

VERDICT (real data: UCI "Individual household electric power consumption", 2.07M minute readings,
7 numeric channels parsed to exact fixed-point int — active/reactive power, voltage, intensity,
3× sub-metering; ~600k-row sample shown; per-channel round-trip verified):
- ❌ **RULED OUT.** The numeric-representation + per-channel Δ lever is real and big vs the
  as-shipped CSV (Δ+`xz -9` = **1.87 MB vs CSV-xz 3.47 MB, +46%**), but **pertype's `ctxcoder`
  loses to plain Δ+xz**: per-ch predict+`ctxcoder` = **2.40 MB = −29%**; `columnar` (w4/w28) the
  same (−29%). Even an oracle per-channel router nets only **+2% over Δ+xz**, and that sliver comes
  from xz on 5/7 channels — not from pertype.
- **Per-channel mechanism (the whole story):** pertype's arithmetic coder *wins* on the two smooth
  **analog power** channels (active/reactive power — continuously-varying residuals, arithmetic's
  home: −4% to −5% vs xz) but *loses badly* on the **sub-metering** channels (integer Wh that sit
  constant for long stretches as an appliance runs/idles → **runs of identical values**: Sub2
  60.6 KB xz vs 196.2 KB ctxcoder = **3.2×** worse) and moderately on voltage/intensity. The
  run-heavy + **daily-periodicity** structure (1440-min cycle repeating day-over-day) is exactly
  what `xz`'s long-window LZ matching captures and a per-symbol order-0/context arithmetic coder
  cannot.
- **Why it differs from the EEG/mocap wins:** those channels are continuously-varying analog where
  every sample differs and the residual is a tight near-Gaussian the arithmetic coder codes
  optimally; a smart-meter record is *dominated* by quantized integer sub-metering with long
  constant runs + a periodic daily template — LZ territory. Same family as the NetFlow / FCS
  rulings: a real representation lever (numeric + Δ) that **generic LZ realizes better than
  pertype** because the data is repetition-heavy, not smooth-residual.

Data: single login-free UCI ZIP (~20 MB) → one CSV (~133 MB). Compares numeric-payload codecs
(int32 channels) and the as-shipped CSV-text codecs in absolute MB.
"""
import gzip, lzma, os, subprocess, sys, urllib.request, zipfile
import numpy as np

URL = "https://archive.ics.uci.edu/static/public/235/individual+household+electric+power+consumption.zip"
DDIR = sys.argv[1] if len(sys.argv) > 1 else "ami_data"
NROWS = int(sys.argv[2]) if len(sys.argv) > 2 else 600_000
CHN = ["ActivePwr", "ReactivePwr", "Voltage", "Intensity", "SubMeter1", "SubMeter2", "SubMeter3"]


def fetch():
    os.makedirs(DDIR, exist_ok=True)
    txt = os.path.join(DDIR, "household_power_consumption.txt")
    if not os.path.exists(txt):
        z = os.path.join(DDIR, "ami.zip")
        if not os.path.exists(z):
            urllib.request.urlretrieve(URL, z)
        with zipfile.ZipFile(z) as zf:
            zf.extractall(DDIR)
    return txt


def zstd(b, l=19):
    return len(subprocess.run(["zstd", f"-{l}", "-c"], input=b, stdout=subprocess.PIPE).stdout)


def xz(b): return len(lzma.compress(b, preset=9))


def load(txt):
    lines = open(txt).read().split("\n")
    body = lines[1:1 + NROWS]
    csv = (lines[0] + "\n" + "\n".join(body)).encode()
    cols = [[] for _ in range(7)]; gaps = 0
    for ln in body:
        if not ln:
            continue
        f = ln.split(";")
        if len(f) < 9 or "?" in f[2:9]:
            gaps += 1; continue
        for c in range(7):
            cols[c].append(int(round(float(f[2 + c]) * 1000)))
    M = np.array(cols, dtype=np.int64)
    return csv, M, gaps


def best_ctx(v, ctxcoder):
    """best of raw / Δ / Δ² value series -> ctxcoder bytes; round-trip checked."""
    d1 = np.concatenate([v[:1], np.diff(v)])
    d2 = np.concatenate([d1[:1], np.diff(d1)])
    best = None; blab = None
    for lab, r in (("raw", v), ("d1", d1), ("d2", d2)):
        blob = ctxcoder.encode(r.tolist())
        back = np.array(ctxcoder.decode(blob, len(r)), np.int64)
        rec = back if lab == "raw" else (np.cumsum(back) if lab == "d1" else np.cumsum(np.cumsum(back)))
        assert (rec == v).all(), f"round-trip {lab}"
        if best is None or len(blob) < best:
            best, blab = len(blob), lab
    return best, blab


def main():
    from pertype import ctxcoder, columnar
    txt = fetch()
    csv, M, gaps = load(txt)
    nch, n = M.shape
    payload = np.ascontiguousarray(M.astype("<i4")).tobytes()
    print(f"{n} valid rows ({gaps} gaps), {nch} ch; CSV {len(csv)/1e6:.1f} MB, int32 payload {len(payload)/1e6:.2f} MB")

    cx = len(lzma.compress(csv, preset=9))
    print(f"\n== as-shipped CSV text ==  gzip {len(csv)/len(gzip.compress(csv,9)):.2f}x  "
          f"zstd {len(csv)/zstd(csv):.2f}x  xz {len(csv)/cx:.2f}x  ({cx/1e6:.2f} MB)", flush=True)

    print("\n per-channel  [pertype ctx | xz(Δ) | zstd(Δ)] KB   winner", flush=True)
    tot_ct = tot_dxz = tot_dzs = tot_route = 0
    for c in range(nch):
        v = M[c]
        bct, lab = best_ctx(v, ctxcoder)
        d = np.concatenate([v[:1], np.diff(v)]).astype("<i4").tobytes()
        dxz, dzs = xz(d), zstd(d)
        tot_ct += bct; tot_dxz += dxz; tot_dzs += dzs; tot_route += min(bct, dxz, dzs)
        win = min([("ctx", bct), ("xz", dxz), ("zstd", dzs)], key=lambda x: x[1])[0]
        print(f"   {CHN[c]:11}({lab}) {bct/1e3:8.1f} | {dxz/1e3:8.1f} | {dzs/1e3:8.1f}   -> {win}", flush=True)

    cm = np.ascontiguousarray(M.T.astype("<i4")).tobytes()
    col = len(columnar.encode(cm, width=4*nch)); assert columnar.decode(columnar.encode(cm, width=4*nch)) == cm

    print(f"\n== numeric totals (vs int32 payload {len(payload)/1e6:.2f} MB) ==", flush=True)
    for k, vv in [("per-ch Δ + xz (DEGA/LZMA-class)", tot_dxz), ("per-ch Δ + zstd", tot_dzs),
                  ("pertype per-ch ctxcoder", tot_ct), ("pertype columnar (record-major)", col),
                  ("oracle per-ch router (best of 3)", tot_route)]:
        print(f"   {k:34} {len(payload)/vv:6.2f}x  ({vv/1e6:.2f} MB)  vs Δ+xz {(1-vv/tot_dxz)*100:+6.1f}%",
              flush=True)


if __name__ == "__main__":
    main()
