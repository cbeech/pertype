"""Silesia corpus, routed per-type — the philosophy on the modern general-purpose standard.

The Silesia corpus (12 files) is the de-facto modern general-compression benchmark. Our
codec is not one general engine but a *router* over per-type specialists, so we score each
file where it belongs and say so honestly:

  mr            -> 3D volume codec (it's a 19x512x512 uint16 DICOM MR stack)
  dickens/webster/reymont/samba/xml/nci -> amortized held-out text model (train/test split)
  sao / x-ray   -> numeric context coder (best of raw / first-difference, int16 view)
  mozilla/ooffice/osdb -> general fallback only (binaries/DB dumps — NOT our design)

Every "ours" number is round-trip verified. Modes:
  python3 scripts/silesia_benchmark.py special <dir>   # mr + numeric + binary reference (fast)
  python3 scripts/silesia_benchmark.py text    <dir>   # held-out text models (slow, ~min/file)
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np

from compressor import ctxcoder, imagecodec

TEXT = ["dickens", "webster", "reymont", "samba", "xml", "nci"]
BINARY = ["mozilla", "ooffice", "osdb"]


def sh(cmd, data):
    return len(subprocess.run(cmd, input=data, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, check=True).stdout)


def std_tools(data):
    return {"gzip": sh(["gzip", "-9", "-c"], data), "bzip2": sh(["bzip2", "-9", "-c"], data),
            "xz": sh(["xz", "-9", "-c"], data), "zstd": sh(["zstd", "-19", "-c"], data)}


def numeric_ours(data):
    """Best of ctxcoder on the raw int16 view or its first difference; round-trip verified."""
    x = np.frombuffer(data[: len(data) // 2 * 2], "<i2").astype(np.int64)
    best, tag = None, None
    for name, arr, inv in (("raw", x, lambda a: a),
                           ("delta", np.concatenate([x[:1], np.diff(x)]), np.cumsum)):
        cb = ctxcoder.encode(arr)
        assert np.array_equal(np.asarray(inv(np.asarray(ctxcoder.decode(cb, len(arr))))), x)
        if best is None or len(cb) < best:
            best, tag = len(cb), name
    return best + (len(data) - len(x) * 2), tag      # + the odd trailing byte, stored


def run_special(d):
    print(f"{'file':<10}{'role':<22}{'raw':>9}{'gzip':>8}{'xz':>8}{'zstd':>8}{'ours':>9}{'note':>8}")
    print("-" * 80)

    # mr -> volume codec
    import pydicom
    from pydicom.uid import ImplicitVRLittleEndian
    ds = pydicom.dcmread(os.path.join(d, "mr"), force=True)
    ds.file_meta = getattr(ds, "file_meta", None) or pydicom.dataset.FileMetaDataset()
    ds.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian
    vol = np.ascontiguousarray(ds.pixel_array.astype(np.int16))     # (19,512,512)
    blob = imagecodec.encode_volume(vol)
    assert np.array_equal(imagecodec.decode_volume(blob), vol), "mr round-trip FAILED"
    raw = open(os.path.join(d, "mr"), "rb").read()
    s = std_tools(raw)
    print(f"{'mr':<10}{'3D volume (uint16)':<22}{len(raw)/1e6:>8.1f}M{s['gzip']/1e6:>7.1f}M"
          f"{s['xz']/1e6:>7.1f}M{s['zstd']/1e6:>7.1f}M{len(blob)/1e6:>8.1f}M"
          f"{'WIN' if len(blob) < min(s.values()) else 'lose':>8}")

    # sao / x-ray -> numeric context coder
    for f, role in (("sao", "float records->numeric"), ("x-ray", "16-bit image->numeric")):
        raw = open(os.path.join(d, f), "rb").read()
        s = std_tools(raw)
        ours, tag = numeric_ours(raw)
        print(f"{f:<10}{role:<22}{len(raw)/1e6:>8.1f}M{s['gzip']/1e6:>7.1f}M"
              f"{s['xz']/1e6:>7.1f}M{s['zstd']/1e6:>7.1f}M{ours/1e6:>8.1f}M"
              f"{('WIN' if ours < min(s.values()) else 'lose')+'/'+tag:>8}")

    # binaries -> reference only (not our design)
    for f in BINARY:
        raw = open(os.path.join(d, f), "rb").read()
        s = std_tools(raw)
        print(f"{f:<10}{'binary (NOT our design)':<22}{len(raw)/1e6:>8.1f}M{s['gzip']/1e6:>7.1f}M"
              f"{s['xz']/1e6:>7.1f}M{s['zstd']/1e6:>7.1f}M{'—':>9}{'ref':>8}")


def run_text(d):
    from compressor.benchmark import _zstd_dict_size, _zstd_dicts
    from compressor.codec import compress, decompress
    from compressor.model import train
    # Train on 1 MB — the dictionary miner's saturation point (max_mining_bytes), now
    # reachable since the blob search is memory-bounded. On source code this is decisive
    # (samba: beats only gzip at 512 KB -> beats every standard tool at 1 MB).
    BLK, NTR, NTE = 32 * 1024, 32, 16
    print(f"{'file':<10}{'ours':>7}{'gzip':>7}{'bzip2':>7}{'xz':>7}{'zstd':>7}"
          f"{'z--tr':>7}   (bits/char, held-out)")
    print("-" * 64)
    import tempfile
    for f in TEXT:
        raw = open(os.path.join(d, f), "rb").read()
        blocks = [raw[i * BLK:(i + 1) * BLK] for i in range(NTR + NTE)]
        tr, te = blocks[:NTR], blocks[NTR:NTR + NTE]
        nbytes = sum(len(b) for b in te)
        model = train(list(tr), type_id=f)
        tot = {"ours": 0, "gzip": 0, "bzip2": 0, "xz": 0, "zstd": 0}
        with tempfile.TemporaryDirectory() as wd:
            dps = _zstd_dicts([(None, b) for b in tr], wd)
            dt = {dp: sum(_zstd_dict_size(b, dp) for b in te) for dp in dps}
            ztr = min(dt.values()) if dt else 0
            for b in te:
                c = compress(b, model)
                assert decompress(c, model) == b, f"round-trip FAILED {f}"
                tot["ours"] += len(c)
                tot["gzip"] += sh(["gzip", "-9", "-c"], b)
                tot["bzip2"] += sh(["bzip2", "-9", "-c"], b)
                tot["xz"] += sh(["xz", "-9", "-c"], b)
                tot["zstd"] += sh(["zstd", "-19", "-c"], b)

        def bpc(x):
            return 8 * x / nbytes
        print(f"{f:<10}{bpc(tot['ours']):>7.3f}{bpc(tot['gzip']):>7.3f}{bpc(tot['bzip2']):>7.3f}"
              f"{bpc(tot['xz']):>7.3f}{bpc(tot['zstd']):>7.3f}{bpc(ztr) if ztr else 0:>7.3f}", flush=True)


def main():
    if len(sys.argv) != 3 or sys.argv[1] not in ("special", "text"):
        sys.exit(__doc__)
    (run_special if sys.argv[1] == "special" else run_text)(sys.argv[2])


if __name__ == "__main__":
    main()
