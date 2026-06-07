"""Throughput benchmark: the Rust port vs the Python codec (which itself uses the C native).

Because the Rust port is byte-identical to Python/C, the *ratios* are equal by construction —
this measures *speed*. For each codec we time encode and decode on realistic synthetic data,
report MB/s for both, and the Rust speedup. "Python" here = Python orchestration + C-native
inner loops (native.HAVE_NATIVE must be True for a fair fight).

Run:  /usr/bin/python3.13 scripts/rust_vs_python_benchmark.py
"""
import ctypes
import glob
import os
import struct
import time

import numpy as np

from compressor import (audiocodec, columnar, csvcolumnar, ctxcoder, floatcodec,
                        imagecodec, native, predictors, videocodec)
from compressor import model as textmodel
from compressor.codec import compress as text_compress
from compressor.codec import decompress as text_decompress

_SO = glob.glob(os.path.join(os.path.dirname(__file__), "..", "rust", "target", "release",
                             "**", "libcompressor_rs.so"), recursive=True)
assert _SO, "build the cdylib first: (cd rust && cargo build --release)"
LIB = ctypes.CDLL(_SO[0])
for name in ("ctx_encode", "calic_codec_encode", "calic_codec_decode", "columnar_encode",
             "columnar_decode", "float_encode", "float_decode", "csv_encode", "csv_decode",
             "image_encode", "image_decode", "audio_encode", "audio_decode",
             "video_encode", "video_decode", "text_compress", "text_decompress"):
    getattr(LIB, name).restype = ctypes.c_long


def _time(fn, reps=3):
    fn()  # warmup
    best = float("inf")
    for _ in range(reps):
        t = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t)
    return best


def _buf(data):
    return (ctypes.c_uint8 * len(data)).from_buffer_copy(data)


def _out(n):
    return (ctypes.c_uint8 * n)()


RESULTS = []


def report(name, nbytes, py_enc, py_dec, ru_enc, ru_dec):
    mb = nbytes / 1e6
    RESULTS.append((name, mb, py_enc, py_dec, ru_enc, ru_dec))


# --- ctxcoder ---------------------------------------------------------------

def bench_ctxcoder():
    rng = np.random.default_rng(0)
    res = np.cumsum(rng.integers(-3, 4, 2_000_000)).astype(np.int64)
    nbytes = res.nbytes
    cap = len(res) * 16 + 1024
    arr = np.ascontiguousarray(res, np.int64)
    out = _out(cap)
    p = arr.ctypes.data_as(ctypes.POINTER(ctypes.c_int64))

    def ru_e():
        return LIB.ctx_encode(p, len(arr), out, cap)
    m = ru_e()
    blob = bytes(out[:m])
    dec_out = (ctypes.c_int64 * len(arr))()
    bb = _buf(blob)

    py_enc = _time(lambda: ctxcoder.encode(res))
    py_dec = _time(lambda: ctxcoder.decode(blob, len(res)))
    ru_enc = _time(ru_e)
    ru_dec = _time(lambda: LIB.ctx_decode(bb, len(blob), len(arr), dec_out))
    report("ctxcoder (2M residuals)", nbytes, py_enc, py_dec, ru_enc, ru_dec)


# --- CALIC image ------------------------------------------------------------

def bench_calic():
    rng = np.random.default_rng(1)
    img = (np.cumsum(rng.integers(-3, 4, (512, 512)), axis=1) % 256).astype(np.int32)
    h, w = img.shape
    nbytes = h * w
    a = np.ascontiguousarray(img, np.int64)
    cap = h * w * 8 + 1024
    out = _out(cap)
    pp = a.ctypes.data_as(ctypes.POINTER(ctypes.c_int64))

    def ru_e():
        return LIB.calic_codec_encode(pp, h, w, 1, out, cap)
    m = ru_e()
    blob = bytes(out[:m])
    bb = _buf(blob)
    dout = (ctypes.c_int64 * (h * w))()
    dp = ctypes.cast(dout, ctypes.POINTER(ctypes.c_int64))

    py_enc = _time(lambda: predictors.calic_full_encode(np.ascontiguousarray(img), 1))
    py_dec = _time(lambda: predictors.calic_full_decode(blob, h, w, 1))
    ru_enc = _time(ru_e)
    ru_dec = _time(lambda: LIB.calic_codec_decode(bb, len(blob), dp, h, w, 1))
    report("CALIC image (512x512)", nbytes, py_enc, py_dec, ru_enc, ru_dec)


# --- columnar ---------------------------------------------------------------

def bench_columnar():
    rng = np.random.default_rng(2)
    n = 80_000
    cols = [np.cumsum(rng.integers(-3, 4, n)).astype("<i4") for _ in range(4)]
    rec = np.empty((n, 16), np.uint8)
    for j in range(4):
        rec[:, j * 4:j * 4 + 4] = cols[j].view(np.uint8).reshape(n, 4)
    data = rec.tobytes()
    nbytes = len(data)
    cap = len(data) * 2 + (1 << 20)
    out = _out(cap)
    bd = _buf(data)

    def ru_e():
        return LIB.columnar_encode(bd, len(data), 16, out, cap)
    m = ru_e()
    blob = bytes(out[:m])
    bb = _buf(blob)
    dout = _out(cap)

    py_enc = _time(lambda: columnar.encode(data, width=16))
    py_dec = _time(lambda: columnar.decode(blob))
    ru_enc = _time(ru_e)
    ru_dec = _time(lambda: LIB.columnar_decode(bb, len(blob), dout, cap))
    report("columnar (16-field, 80k recs)", nbytes, py_enc, py_dec, ru_enc, ru_dec)


# --- floatcodec -------------------------------------------------------------

def bench_floatcodec():
    rng = np.random.default_rng(3)
    grid = (np.cumsum(rng.integers(-3, 4, 300_000)) / 100.0).astype("<f4")
    data = np.ascontiguousarray(grid).tobytes()
    nbytes = len(data)
    cap = len(data) * 2 + (1 << 20)
    out = _out(cap)
    bd = _buf(data)

    def ru_e():
        return LIB.float_encode(bd, len(data), 4, out, cap)
    m = ru_e()
    blob = bytes(out[:m])
    bb = _buf(blob)
    dout = _out(cap)

    py_enc = _time(lambda: floatcodec.encode(data, 4))
    py_dec = _time(lambda: floatcodec.decode(blob))
    ru_enc = _time(ru_e)
    ru_dec = _time(lambda: LIB.float_decode(bb, len(blob), dout, cap))
    report("floatcodec (300k f32)", nbytes, py_enc, py_dec, ru_enc, ru_dec)


# --- csvcolumnar ------------------------------------------------------------

def bench_csv():
    rows = ["t;v;n"]
    v = 1000
    for i in range(60_000):
        v += (i * 7 % 11) - 5
        rows.append(f"2024-01-01;{v / 100:.2f};{i}")
    data = ("\n".join(rows) + "\n").encode()
    nbytes = len(data)
    cap = len(data) * 2 + (1 << 20)
    out = _out(cap)
    bd = _buf(data)

    def ru_e():
        return LIB.csv_encode(bd, len(data), out, cap)
    m = ru_e()
    blob = bytes(out[:m])
    bb = _buf(blob)
    dout = _out(cap)

    py_enc = _time(lambda: csvcolumnar.encode(data))
    py_dec = _time(lambda: csvcolumnar.decode(blob))
    ru_enc = _time(ru_e)
    ru_dec = _time(lambda: LIB.csv_decode(bb, len(blob), dout, cap))
    report("csvcolumnar (60k rows)", nbytes, py_enc, py_dec, ru_enc, ru_dec)


# --- imagecodec (full: MED/CALIC/RLE selection) -----------------------------

def bench_imagecodec():
    rng = np.random.default_rng(4)
    rgb = (np.cumsum(rng.integers(-4, 5, (384, 384, 3)), axis=1) % 256).astype(np.uint8)
    data = np.ascontiguousarray(rgb).tobytes()
    h, w = rgb.shape[:2]
    nbytes = len(data)
    cap = len(data) + (1 << 18)
    out = _out(cap)
    bd = _buf(data)

    def ru_e():
        return LIB.image_encode(bd, len(data), h, w, 1, 2, 0, out, cap)
    m = ru_e()
    blob = bytes(out[:m])
    bb = _buf(blob)
    dout = _out(cap)

    py_enc = _time(lambda: imagecodec.encode(rgb))
    py_dec = _time(lambda: imagecodec.decode(blob))
    ru_enc = _time(ru_e)
    ru_dec = _time(lambda: LIB.image_decode(bb, len(blob), dout, cap))
    report("imagecodec (384x384 RGB)", nbytes, py_enc, py_dec, ru_enc, ru_dec)


# --- audiocodec -------------------------------------------------------------

def bench_audio():
    rng = np.random.default_rng(5)
    n = 200_000
    L = np.clip(np.cumsum(rng.integers(-30, 31, n)), -30000, 30000).astype(np.int16)
    R = np.clip(L + np.cumsum(rng.integers(-8, 9, n)), -30000, 30000).astype(np.int16)
    pcm = np.stack([L, R], axis=1).astype(np.int16)
    flat = np.ascontiguousarray(pcm).reshape(-1)
    nbytes = flat.nbytes
    cap = flat.size * 4 + (1 << 20)
    out = _out(cap)
    pp = np.ascontiguousarray(flat, np.int16).ctypes.data_as(ctypes.POINTER(ctypes.c_int16))

    def ru_e():
        return LIB.audio_encode(pp, n, 2, 44100, 0, out, cap)  # rice
    m = ru_e()
    blob = bytes(out[:m])
    bb = _buf(blob)
    dout = (ctypes.c_int16 * (n * 2 + 16))()

    py_enc = _time(lambda: audiocodec.encode(pcm, 44100, coder="rice"))
    py_dec = _time(lambda: audiocodec.decode(blob))
    ru_enc = _time(ru_e)
    ru_dec = _time(lambda: LIB.audio_decode(bb, len(blob), dout, n * 2 + 16))
    report("audiocodec (200k stereo, rice)", nbytes, py_enc, py_dec, ru_enc, ru_dec)


# --- videocodec -------------------------------------------------------------

def bench_video():
    rng = np.random.default_rng(6)
    T, H, W = 12, 144, 176
    base = (np.add.outer(np.arange(H), np.arange(W)) % 256).astype(np.int64)
    frames = np.empty((T, H, W), np.uint8)
    for t in range(T):
        s = np.roll(base, (t, 2 * t), axis=(0, 1))
        frames[t] = ((s + rng.integers(-4, 5, (H, W))) % 256).astype(np.uint8)
    data = np.ascontiguousarray(frames).tobytes()
    nbytes = len(data)
    cap = len(data) + (1 << 20)
    out = _out(cap)
    bd = _buf(data)

    def ru_e():
        return LIB.video_encode(bd, T, H, W, out, cap)
    m = ru_e()
    blob = bytes(out[:m])
    bb = _buf(blob)
    dout = _out(T * H * W)

    py_enc = _time(lambda: videocodec.encode(frames), reps=2)
    py_dec = _time(lambda: videocodec.decode(blob), reps=2)
    ru_enc = _time(ru_e)
    ru_dec = _time(lambda: LIB.video_decode(bb, len(blob), dout, T * H * W))
    report("videocodec (12x144x176, QCIF)", nbytes, py_enc, py_dec, ru_enc, ru_dec)


# --- textcodec --------------------------------------------------------------

def bench_text():
    # LZ cost-optimal model with a blob (the float-priced optimal parse — the slow path)
    html = [b"<html><head><title>Page %d</title></head><body><p>lorem %d ipsum dolor sit "
            b"amet consectetur adipiscing</p></body></html>" % (i, i * 3) for i in range(200)]
    blob = textmodel._build_blob(html, cap=1 << 15)
    d, mm, dm, mo = textmodel._artifacts(html, blob, 1024, 3, 256, textmodel.MAX_CHAIN)
    model = textmodel.Model(type_id="html", dictionary=d, blob=blob, main_model=mm,
                            dist_model=dm, mode_model=mo, transform=(), use_lz=True)
    mb = model.save()
    data = b"".join(html) + b"<html><head><title>Page X</title></head><body><p>novel</p></body></html>" * 50
    nbytes = len(data)
    cap = len(data) * 4 + (1 << 20)
    out = _out(cap)
    mbuf = _buf(mb)
    dbuf = _buf(data)

    def ru_e():
        return LIB.text_compress(mbuf, len(mb), dbuf, len(data), out, cap)
    m = ru_e()
    cz = bytes(out[:m])
    cb = _buf(cz)
    dout = _out(cap)

    py_enc = _time(lambda: text_compress(data, model), reps=2)
    py_dec = _time(lambda: text_decompress(cz, model), reps=2)
    ru_enc = _time(ru_e)
    ru_dec = _time(lambda: LIB.text_decompress(mbuf, len(mb), cb, len(cz), dout, cap))
    report("textcodec (~50KB, LZ-optimal)", nbytes, py_enc, py_dec, ru_enc, ru_dec)


def main():
    assert native.HAVE_NATIVE, "C native not built — comparison would be unfair (pure-Python)"
    print(f"cdylib: {os.path.relpath(_SO[0])}\nPython: {os.sys.version.split()[0]} "
          f"(C native: {native.HAVE_NATIVE})\n")
    for fn in (bench_ctxcoder, bench_calic, bench_columnar, bench_floatcodec, bench_csv,
               bench_imagecodec, bench_audio, bench_video, bench_text):
        fn()

    hdr = (f"{'codec':<32}{'MB':>7}{'py enc':>10}{'ru enc':>10}{'enc x':>7}"
           f"{'py dec':>10}{'ru dec':>10}{'dec x':>7}")
    print(hdr)
    print("-" * len(hdr))
    for name, mb, pe, pd, re, rd in RESULTS:
        print(f"{name:<32}{mb:>7.2f}"
              f"{mb / pe:>9.1f}M{mb / re:>9.1f}M{pe / re:>6.1f}x"
              f"{mb / pd:>9.1f}M{mb / rd:>9.1f}M{pd / rd:>6.1f}x")
    print("\n(MB/s throughput on the uncompressed input; 'x' = Rust speedup. "
          "Ratios are identical — the port is byte-identical.)")


if __name__ == "__main__":
    main()
