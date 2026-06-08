"""Training throughput: the Rust trainer (`train_model`) vs Python's `model.train`.

Training is the heaviest path. The two are byte-identical where the zlib transform proxy
agrees (verified in tests), so this is a pure speed comparison. Note the asymmetry:

  * Python  = pure-Python mining/blob + C-native parse + a multiprocessing pool for the
              blob-strategy search (forked when the fit slice >= 512 KB).
  * Rust    = all-native, but single-threaded (serial blob search).

So on small corpora (serial both sides) Rust should win cleanly; on large corpora Python's
process pool claws back wall-time via cores the Rust trainer doesn't use yet (rayon over the
blob search is the obvious follow-up).

Run:  PYTHONPATH=. python3 scripts/rust_vs_python_train_benchmark.py
"""
import ctypes
import glob
import math
import os
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from compressor import model as textmodel
from compressor.model import Model

_SO = glob.glob(os.path.join(os.path.dirname(__file__), "..", "rust", "target", "release",
                             "**", "libcompressor_rs.so"), recursive=True)
assert _SO, "build the cdylib first: (cd rust && cargo build --release)"
LIB = ctypes.CDLL(_SO[0])
LIB.train_model.restype = ctypes.c_long


def rust_train(samples, type_id, mp=256, mil=3, mxl=256):
    flat = b"".join(samples)
    lens = (ctypes.c_int64 * len(samples))(*[len(s) for s in samples])
    data = (ctypes.c_uint8 * max(1, len(flat))).from_buffer_copy(flat or b"\x00")
    tb = type_id.encode()
    tbuf = (ctypes.c_uint8 * len(tb)).from_buffer_copy(tb)
    out = (ctypes.c_uint8 * (16 << 20))()
    m = LIB.train_model(data, lens, len(samples), tbuf, len(tb), mp, mil, mxl, out, len(out))
    assert m >= 0
    return bytes(out[:m])


def corpora():
    json = [b'{"name":"item%d","value":%d,"ok":true,"tags":["a","b"]}' % (i, i * 7)
            for i in range(400)]
    logs = [b"2024-01-01T00:00:%02d record %04d | host=server-%02d | status=ok\n"
            % (i % 60, i, i % 16) for i in range(600)]
    http = [b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nServer: nginx\r\n\r\n"
            b'{"id":%d,"user":"user_%d","data":"%s"}' % (i, i % 80, (b"payload_%d_" % i) * 4)
            for i in range(1200)]
    flt = [b"".join(struct.pack("<d", math.sin((i + k) * 0.01) * 1000.0) for i in range(300))
           for k in range(40)]
    # All fit slices here are < 512 KB, so Python stays serial (no process pool) — a clean
    # serial-vs-serial comparison. The pool (and a rayon Rust search) only matter past that.
    return [("json", json, 256), ("logs", logs, 256), ("http", http, 512),
            ("float64", flt, 256)]


def main():
    print(f"cores: {os.cpu_count()}   cdylib: {os.path.relpath(_SO[0])}\n")
    hdr = f"{'corpus':<14}{'samples':>8}{'KB':>8}{'use_lz':>7}{'py s':>9}{'rust s':>9}{'speedup':>9}"
    print(hdr)
    print("-" * len(hdr))
    for name, samples, mp in corpora():
        kb = sum(len(s) for s in samples) / 1024
        t = time.perf_counter()
        pm = textmodel.train(samples, type_id=name.split("(")[0], max_patterns=mp)
        py = time.perf_counter() - t
        pb = pm.save()
        t = time.perf_counter()
        rb = rust_train(samples, name.split("(")[0], mp)
        ru = time.perf_counter() - t
        tag = "" if pb == rb else "  (transform proxy differs)"
        print(f"{name:<14}{len(samples):>8}{kb:>8.0f}{str(pm.use_lz):>7}"
              f"{py:>9.2f}{ru:>9.2f}{py / ru:>8.1f}x{tag}")
    print("\n(byte-identical output where the zlib transform proxy agrees. All fit slices "
          "here are < 512 KB so Python ran serial — a fair serial-vs-serial comparison. The "
          "Rust win is largest where pure-Python mining/blob dominates, smallest where the "
          "C-native parse does. Past 512 KB Python's process pool would narrow the gap; rayon "
          "over the Rust blob search is the matching follow-up.)")


if __name__ == "__main__":
    main()
