"""Test the full pipeline on all colour planes (Y, U, V), not just luma.

The .y4m clips are 4:2:0, so U and V are quarter-resolution chroma planes. We run
the finished inter-frame coder (quarter-pel MC + per-block SKIP/INTER/INTRA with
MED intra, from video_qpel_benchmark) independently on each plane and compare the
combined YUV size to per-frame intra-only JPEG-XL on each plane. Every plane is
round-trip verified inside the pipeline.

Usage: python3 scripts/video_color_benchmark.py [n_frames]
"""
import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))                 # sibling import
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # compressor

import numpy as np
import imagecodecs as ic

import video_qpel_benchmark as q   # reuse run()/predict_qpel/etc (block 16, search +/-8)

NF = int(sys.argv[1]) if len(sys.argv) > 1 else 60


def read_y4m_yuv(path, n):
    raw = open(path, "rb").read()
    nl = raw.index(b"\n")
    W = H = None
    for tok in raw[:nl].decode("ascii").split():
        if tok[0] == "W":
            W = int(tok[1:])
        elif tok[0] == "H":
            H = int(tok[1:])
    ys, cs = W * H, (W // 2) * (H // 2)
    fs = ys + 2 * cs
    pos, Ys, Us, Vs = nl + 1, [], [], []
    while pos < len(raw) and len(Ys) < n:
        pos = raw.index(b"\n", pos) + 1
        Ys.append(np.frombuffer(raw[pos:pos + ys], np.uint8).reshape(H, W))
        Us.append(np.frombuffer(raw[pos + ys:pos + ys + cs], np.uint8).reshape(H // 2, W // 2))
        Vs.append(np.frombuffer(raw[pos + ys + cs:pos + ys + 2 * cs], np.uint8).reshape(H // 2, W // 2))
        pos += fs
    return np.stack(Ys), np.stack(Us), np.stack(Vs)


def intra_jxl(stack):
    return sum(len(ic.jpegxl_encode(np.ascontiguousarray(stack[t].astype(np.uint8)),
                                    lossless=True)) for t in range(len(stack)))


def main():
    print(f"frames={NF}   (per-plane: intra-JXL -> ours)")
    print(f"{'clip':<12}{'plane':>6}{'intra-JXL':>11}{'ours':>11}{'gain':>8}")
    print("-" * 48)
    for path in sorted(glob.glob("/home/user/sci_data/video/*.y4m")):
        name = path.split("/")[-1].replace(".y4m", "")
        planes = dict(zip("YUV", read_y4m_yuv(path, NF)))
        raw = sum(p.nbytes for p in planes.values())
        ti = to = 0
        for pn, st in planes.items():
            st = st.astype(np.int64)
            it = intra_jxl(st)
            ou = q.run(st, quarter=True)
            ti += it; to += ou
            print(f"{name:<12}{pn:>6}{it/1e6:>10.3f}M{ou/1e6:>10.3f}M{(it-ou)/it*100:>+7.0f}%")
        print(f"{name:<12}{'TOTAL':>6}{ti/1e6:>10.3f}M{to/1e6:>10.3f}M{(ti-to)/ti*100:>+7.0f}%"
              f"   (vs raw YUV {raw/to:.2f}x; intra {raw/ti:.2f}x)")
        print("-" * 48)


if __name__ == "__main__":
    main()
