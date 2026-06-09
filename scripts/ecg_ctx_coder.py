"""Prototype: context-adaptive arithmetic coding of a prediction residual.

Step-2 attempt to beat xz on ECG. The residual-entropy analysis showed our
memoryless Rice coder (6.37 b/s) sits far above the order-0 ceiling (5.46) while
a coder that conditions on the previous residual's magnitude can reach ~5.03 b/s
— below xz (5.39). So the fix is not RLE/LZ but a *context-adaptive entropy
coder*.

Scheme per residual r (after delta): zigzag to u; emit magnitude bucket
k = u.bit_length() with an adaptive arithmetic model selected by the PREVIOUS
bucket (context), then the k-1 low "mantissa" bits raw (the leading 1 is
implicit). Encoder/decoder update counts in lockstep, so nothing is transmitted.

Round-trip verified, compared against xz -9, on the Apnea-ECG records.
"""
import os
import glob
import subprocess
import time

import numpy as np

from compressor.arithmetic import ArithmeticEncoder, ArithmeticDecoder
from compressor.bitio import BitReader

NB = 20            # buckets 0..19 (alphabet)
RESCALE = 1 << 14  # halve counts when a context's total reaches this


def _zig(r):
    return (r << 1) ^ (r >> 63)

def _unzig(u):
    return (u >> 1) ^ -(u & 1)


def encode(samples):
    """samples: int64 1-D array -> bytes."""
    d = samples.copy()
    d[1:] = samples[1:] - samples[:-1]          # d[0] = samples[0] (raw seed)
    enc = ArithmeticEncoder()
    freq = [[1] * NB for _ in range(NB)]
    tot = [NB] * NB
    ctx = 0
    for r in d.tolist():
        u = _zig(int(r))
        k = u.bit_length()                       # 0 for u==0
        f = freq[ctx]
        cum = 0
        for s in range(k):
            cum += f[s]
        enc.encode(cum, f[k], tot[ctx])
        if k >= 2:
            enc.encode_bits(u & ((1 << (k - 1)) - 1), k - 1)
        # adaptive update
        f[k] += 32
        tot[ctx] += 32
        if tot[ctx] >= RESCALE:
            t = 0
            for s in range(NB):
                f[s] = (f[s] + 1) >> 1
                t += f[s]
            tot[ctx] = t
        ctx = k
    enc.finish()
    return enc.getvalue()


def decode(blob, n):
    dec = ArithmeticDecoder(BitReader(blob))
    freq = [[1] * NB for _ in range(NB)]
    tot = [NB] * NB
    ctx = 0
    d = np.empty(n, dtype=np.int64)
    for i in range(n):
        f = freq[ctx]
        target = dec.decode_target(tot[ctx])
        cum = 0
        k = 0
        while cum + f[k] <= target:
            cum += f[k]
            k += 1
        dec.update(cum, f[k], tot[ctx])
        if k == 0:
            u = 0
        elif k == 1:
            u = 1
        else:
            mant = dec.decode_bits(k - 1)
            u = (1 << (k - 1)) | mant
        d[i] = _unzig(u)
        f[k] += 32
        tot[ctx] += 32
        if tot[ctx] >= RESCALE:
            t = 0
            for s in range(NB):
                f[s] = (f[s] + 1) >> 1
                t += f[s]
            tot[ctx] = t
        ctx = k
    return np.cumsum(d).astype(np.int64)


def main():
    files = sorted(glob.glob(os.environ.get("SCI_DATA", "data/sci") + "/ecg/*.dat"))
    raw_total = ours_total = xz_total = 0
    t0 = time.time()
    for f in files:
        a = np.fromfile(f, dtype="<i2").astype(np.int64)
        blob = encode(a)
        back = decode(blob, len(a))
        assert np.array_equal(back, a), f"ROUND-TRIP FAILED {f}"
        raw = a.astype("<i2").tobytes()
        xz = len(subprocess.run(["xz", "-9", "-c"], input=raw, stdout=subprocess.PIPE).stdout)
        raw_total += len(raw); ours_total += len(blob); xz_total += xz
        print(f"  {f.split('/')[-1]}: ours {len(blob)*8/len(a):.2f} b/s ({len(raw)/len(blob):.2f}x)  "
              f"xz {xz*8/len(a):.2f} b/s ({len(raw)/xz:.2f}x)")
    print(f"\nTOTAL  raw {raw_total/1e6:.1f}MB")
    print(f"  ours (ctx-arith): {ours_total/1e6:7.3f}MB  ratio {raw_total/ours_total:.3f}x")
    print(f"  xz -9          : {xz_total/1e6:7.3f}MB  ratio {raw_total/xz_total:.3f}x")
    print(f"  round-trip: OK   ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
