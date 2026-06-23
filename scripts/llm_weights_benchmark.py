"""Measure-first: lossless compression of LLM weights (bf16/fp16 checkpoints).

NOTE ON SCOPE: this is about shrinking model *downloads / archives*, NOT making models run on a
laptop. Inference needs weights uncompressed in RAM, so a smaller file buys zero runtime memory or
speed — that's quantization's job (lossy), not this. This probe only asks: can we losslessly beat
the generic/specialist bar on full-precision weight files?

Finding (real Qwen2.5-0.5B bf16, 64 MB of transformer weights): VERDICT — not worth building.
- bf16 byte-planes: the high byte (sign + top-7 exponent bits) has ~2.8-bit entropy; the low byte
  (1 exp bit + 7 mantissa) is ~7.97 bits = near-random/incompressible.
- The lever is byte-plane splitting (ZipNN's insight): split + zstd = ~1.46× (+32% vs raw 1.30×).
- pertype's existing `ctxcoder` is a residual-magnitude coder and FAILS here (~1.0×).
- A proper symbol entropy coder (built on the arithmetic coder) reaches ~1.47× — only **+0.9%**
  over ZipNN-style split+zstd, and order-1/2 context on the exponent does NOT help (near-i.i.d.).
  The incompressible mantissa caps the whole thing near 1.5×.
- On already-quantized weights (GGUF Q4) it's ~0 (high entropy).
So: ZipNN already sits at the entropy floor; pertype offers no edge. Distribution-only anyway.

Bar: gzip/zstd/xz on raw, and byte-split + zstd (≈ ZipNN). Set LLM_WEIGHTS to a raw bf16 blob
(e.g. a range-download of a .safetensors data section).
"""
import os
import subprocess

import numpy as np

LW = os.environ.get("LLM_WEIGHTS", "data/llm/w.bf16")


def sh(data, cmd):
    return len(subprocess.run(cmd, input=data, stdout=subprocess.PIPE).stdout)


def H(a):
    c = np.bincount(a, minlength=256) / len(a); c = c[c > 0]
    return float(-(c * np.log2(c)).sum())


def main():
    raw = open(LW, "rb").read()
    n = len(raw)
    B = np.frombuffer(raw, np.uint8).reshape(-1, 2)
    lo = np.ascontiguousarray(B[:, 0]); hi = np.ascontiguousarray(B[:, 1])  # bf16 LE
    print(f"bf16 weights: {n/1e6:.1f} MB   byte entropy: hi(sign+exp) {H(hi):.2f} b  "
          f"lo(mantissa) {H(lo):.2f} b  (raw 8.00)\n")

    def row(name, s):
        print(f"  {name:32}{n/s:6.2f}x  ({(1-s/n)*100:+.0f}% saved)")

    row("gzip -9 (raw)", sh(raw, ["gzip", "-9"]))
    row("zstd -19 (raw)", sh(raw, ["zstd", "-19", "-c"]))
    row("xz -9 (raw)", sh(raw, ["xz", "-9", "-c"]))
    split = hi.tobytes() + lo.tobytes()
    bar = sh(split, ["zstd", "-19", "-c"]); row("byte-split + zstd (~ZipNN)", bar)
    row("byte-split + xz", sh(split, ["xz", "-9", "-c"]))
    print(f"\nVerdict: byte-split is the lever (ZipNN's insight). A proper entropy coder gains only "
          f"~+1% over split+zstd and order-N context doesn't help (exponent ~i.i.d., mantissa "
          f"incompressible). Not a pertype opportunity; distribution-only, ~0 on quantized weights.")


if __name__ == "__main__":
    main()
