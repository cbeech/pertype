"""Measure-first: flow-cytometry FCS event matrices (real public FCS2.0 + FCS3.0 files) — Tier-2.

Premise: an FCS file's DATA segment is a (events × parameters) matrix — scatter + fluorescence
channels per cell — stored event-major and shipped raw or ZIP'd. The hope was a per-column entropy
lever (range-limited integer / quasi-integer channels, the cryo-EM/ephys "decorrelated → pure
entropy" family) applied via pertype's `ctxcoder` / `floatcodec` / `columnar`.

VERDICT (real data: 3 legacy FCS2.0 16-bit-int files from `RGLab/flowCore`; 3 modern FCS3.0 float32
files from `RGLab/flowWorkspaceData`; round-trip verified):
- ❌ **RULED OUT.** On BOTH integer and float data pertype loses to a *trivial* generic bar —
  **column-major reorder + `xz -9`** (de-interleave the parameters into contiguous columns, then
  generic LZ):
  - legacy int: colmajor+xz **2.01×** vs pertype per-col `ctxcoder` 1.93× / `columnar` 1.93× (−4%).
  - modern float32: colmajor+xz **1.75–2.40×** vs pertype `floatcodec` 1.38–1.87× (**−27 to −40%**),
    `columnar` w4 even worse (−37 to −58%).
  pertype DOES beat the as-shipped zip/gzip (1.18–1.57×) — but that is beating the *storage form*,
  not the specialist (meta-lesson #3).
- **Why there is no lever:** an FCS matrix is **decorrelated per-cell features** — consecutive
  events are independent cells, so columns are NOT temporally smooth (only `Time` is monotonic).
  No prediction lever exists: `columnar`/`floatcodec` Δ *backfires*, and the compensated float32
  values are only 12–55% integer-valued so `floatcodec`'s distinct-value dict blows out. The single
  real redundancy is **cross-event value repetition within a column**, which generic `xz` on the
  column-major layout captures directly and pertype's per-column order-0 arithmetic / byte-
  de-interleave cannot match.
- **The win is a layout, not a codec** (same lesson as scRNA-seq's VCSC/IVCSC and NetFlow): storing
  the matrix column-major — i.e. as **Parquet/Arrow** (dict + RLE + delta + byte-stream-split per
  column) — is the real specialist bar, and it already beats pertype. Same family as the NetFlow
  ruling (decorrelated records where generic LZ on a reordered layout wins) and the masspec-
  intensity / ambisonic float cases.

Data: public FCS files fetched over plain HTTPS from the RGLab GitHub repos (no login).
"""
import gzip, lzma, os, subprocess, sys, urllib.request
import numpy as np

FILES = [  # (url, local) — 3 legacy int (FCS2.0) + 3 modern float (FCS3.0)
    ("https://raw.githubusercontent.com/RGLab/flowCore/master/inst/extdata/0877408774.B08", "leg_B08.fcs"),
    ("https://raw.githubusercontent.com/RGLab/flowCore/master/inst/extdata/0877408774.E07", "leg_E07.fcs"),
    ("https://raw.githubusercontent.com/RGLab/flowCore/master/inst/extdata/0877408774.F06", "leg_F06.fcs"),
    ("https://github.com/RGLab/flowWorkspaceData/raw/master/inst/extdata/CytoTrol_CytoTrol_1.fcs", "mod_CytoTrol1.fcs"),
    ("https://github.com/RGLab/flowWorkspaceData/raw/master/inst/extdata/a2004_O1T2pb05i_A1_A01.fcs", "mod_a2004.fcs"),
    ("https://github.com/RGLab/flowWorkspaceData/raw/master/inst/extdata/diva/124500.fcs", "mod_124500.fcs"),
]
DDIR = sys.argv[1] if len(sys.argv) > 1 else "fcs_data"


def fetch():
    os.makedirs(DDIR, exist_ok=True)
    out = []
    for url, name in FILES:
        p = os.path.join(DDIR, name)
        if not os.path.exists(p):
            urllib.request.urlretrieve(url, p)
        out.append(p)
    return out


def parse_fcs(buf):
    def i(a, b): return int(buf[a:b].decode().strip() or "0")
    t0, t1, d0, d1 = i(10, 18), i(18, 26), i(26, 34), i(34, 42)
    text = buf[t0:t1 + 1].decode("latin1"); delim = text[0]
    parts = text[1:].split(delim)
    kw = {parts[k].upper(): parts[k + 1] for k in range(0, len(parts) - 1, 2)}
    if d0 == 0:
        d0, d1 = int(kw.get("$BEGINDATA", 0)), int(kw.get("$ENDDATA", 0))
    par, tot, dt = int(kw["$PAR"]), int(kw["$TOT"]), kw["$DATATYPE"].upper()
    little = kw.get("$BYTEORD", "1,2,3,4").startswith("1,2")
    bits = [int(kw[f"$P{p}B"]) for p in range(1, par + 1)]
    return par, tot, dt, little, bits, buf[d0:d1 + 1]


def zstd(b, l=19):
    return len(subprocess.run(["zstd", f"-{l}", "-c"], input=b, stdout=subprocess.PIPE).stdout)


def xz(b):
    return len(lzma.compress(b, preset=9))


def shuf_zstd(b, w):
    return zstd(np.frombuffer(b, np.uint8).reshape(-1, w).T.tobytes())


def main():
    from pertype import ctxcoder, floatcodec, columnar
    for fn in fetch():
        par, tot, dt, little, bits, raw = parse_fcs(open(fn, "rb").read())
        end = "<" if little else ">"
        if dt == "I" and len(set(bits)) == 1:
            w = bits[0] // 8
            M = np.frombuffer(raw[:tot*par*w], f"{end}u{w}").reshape(tot, par).astype(f"<u{w}")
        elif dt == "F":
            w = 4; M = np.frombuffer(raw[:tot*par*4], f"{end}f4").reshape(tot, par)
        else:
            print(f"{os.path.basename(fn)}: {dt}{bits[0]} unsupported"); continue
        data = np.ascontiguousarray(M.astype(f"<{'u' if dt=='I' else 'f'}{w}")).tobytes()  # event-major
        colmaj = np.ascontiguousarray(M.T.astype(f"<{'u' if dt=='I' else 'f'}{w}")).tobytes()

        # pertype candidate (best entropy path for the dtype), round-trip verified
        if dt == "I":
            tot_ct = 0
            for c in range(par):
                v = M[:, c].astype(np.int64)
                blob = ctxcoder.encode(v.tolist())
                assert (np.array(ctxcoder.decode(blob, len(v)), np.int64) == v).all()
                tot_ct += len(blob)
            pt, ptlbl = tot_ct, "per-col ctxcoder"
        else:
            blob = floatcodec.encode(colmaj, 4); assert floatcodec.decode(blob) == colmaj
            pt, ptlbl = len(blob), "floatcodec (colmaj)"
        colr = len(columnar.encode(colmaj, width=w)); assert columnar.decode(columnar.encode(colmaj, width=w)) == colmaj

        bars = {"gzip-9 (zip the FCS)": len(gzip.compress(data, 9)), "zstd-19": zstd(data),
                "xz-9 (event-major)": xz(data), "shuffle+zstd": shuf_zstd(data, w),
                "colmajor+xz": xz(colmaj), "colmajor+shuffle+zstd": shuf_zstd(colmaj, w)}
        best_bar = min(bars["xz-9 (event-major)"], bars["shuffle+zstd"],
                       bars["colmajor+xz"], bars["colmajor+shuffle+zstd"])
        print(f"\n{os.path.basename(fn):20} {tot}x{par} {dt}{bits[0]}  ({len(data)/1e6:.1f} MB)", flush=True)
        for lbl, v in bars.items():
            print(f"   {lbl:30} {len(data)/v:5.2f}x   vs best-bar {(1-v/best_bar)*100:+6.1f}%", flush=True)
        for lbl, v in [(f"pertype {ptlbl}", pt), (f"pertype columnar w{w}", colr)]:
            print(f"   {lbl:30} {len(data)/v:5.2f}x   vs best-bar {(1-v/best_bar)*100:+6.1f}%", flush=True)


if __name__ == "__main__":
    main()
