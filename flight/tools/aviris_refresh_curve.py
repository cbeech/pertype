#!/usr/bin/env python3
"""Reproduce the SPECTRAL refresh-band price curve documented in
docs/mission-safety.md §2.5.1 (AVIRIS Indian Pines table) from a FRESH public
download of the scene — no committed data, no hand-carried fixtures.

Acquisition recipe (what this script automates):

  1. The scene is the public AVIRIS Indian Pines hyperspectral cube (200 bands,
     145x145, uint16) in the standard .mat conversion distributed by the EHU GIC
     (Grupo de Inteligencia Computacional, University of the Basque Country):
         https://www.ehu.eus/ccwintco/uploads/6/67/Indian_pines_corrected.mat
     (~5.95 MB MATLAB v5 file, variable `indian_pines_corrected`, stored as
     (H, W, Z) — this script transposes to (Z, H, W) before coding).
  2. That host currently 403s scripted fetches, so the working fetch goes via
     the Internet Archive Wayback Machine with a bare "/web/2id_/" timestamp,
     which redirects to the nearest snapshot of the same URL:
         https://web.archive.org/web/2id_/https://www.ehu.es/ccwintco/uploads/6/67/Indian_pines_corrected.mat
  3. The download is verified against the known sha256 before measuring:
         ec2f8808710919d566f70f0d4aa885aae1ddfd42b734aba71c5e12ca65450939
         (5_953_527 bytes)
     The 2026-08-19 measurement behind §2.5.1 and the 2026-09-09 reproduction
     downloaded byte-identical files with this hash.

Requires numpy + scipy; builds/reuses flight/build/libpfc.so (run
`make -C flight sharedlib` first if the build tree is clean).

    python3 flight/tools/aviris_refresh_curve.py           # download (cached) + measure + print
    python3 flight/tools/aviris_refresh_curve.py --check   # also diff vs the documented §2.5.1 table
"""

import argparse
import ctypes as C
import hashlib
import os
import subprocess
import sys
import urllib.request

import numpy as np
import scipy.io as sio

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
FLIGHT = os.path.join(REPO_ROOT, "flight")
LIB_PATH = os.path.join(FLIGHT, "build", "libpfc.so")
CACHE_DIR = os.path.join(FLIGHT, "build", "aviris")
MAT_NAME = "Indian_pines_corrected.mat"
MAT_PATH = os.path.join(CACHE_DIR, MAT_NAME)

# Ordered (most robust first): the Wayback "nearest snapshot" form, then the
# canonical URL in case the 403 on scripted fetches is ever lifted.
URLS = [
    "https://web.archive.org/web/2id_/https://www.ehu.es/ccwintco/uploads/6/67/Indian_pines_corrected.mat",
    "https://www.ehu.eus/ccwintco/uploads/6/67/Indian_pines_corrected.mat",
]
EXPECTED_SHA256 = "ec2f8808710919d566f70f0d4aa885aae1ddfd42b734aba71c5e12ca65450939"
EXPECTED_BYTES = 5953527

PFC_CODEC_SPECTRAL = 5
INTERVALS = [0, 2, 4, 6, 8, 10, 20, 25, 40, 50, 100, 200]

# Documented §2.5.1 table (mission-safety.md): refresh -> (ratio, cost vs off %).
DOCUMENTED = {
    0: (2.347, 0.00), 2: (2.179, 7.68), 4: (2.257, 3.96), 6: (2.287, 2.60),
    8: (2.301, 1.98), 10: (2.313, 1.44), 20: (2.331, 0.67), 25: (2.334, 0.53),
    40: (2.338, 0.36), 50: (2.343, 0.14), 100: (2.346, 0.03), 200: (2.347, 0.00),
}
RATIO_TOL = 0.005   # measurement noise for a sha256-identical scene is zero;
COST_TOL = 0.10     # the tolerance absorbs .mat revisions with different provenance.


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def acquire():
    """Return the path to a hash-verified local copy, downloading if needed."""
    if os.path.exists(MAT_PATH):
        digest = sha256(MAT_PATH)
        if digest == EXPECTED_SHA256:
            print(f"cache hit: {MAT_PATH}")
            return MAT_PATH
        print(f"cache file hash mismatch ({digest}); re-downloading", file=sys.stderr)
    os.makedirs(CACHE_DIR, exist_ok=True)
    for url in URLS:
        print(f"downloading {url}")
        req = urllib.request.Request(url, headers={"User-Agent": "pfc-aviris-repro/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=300) as r, open(MAT_PATH, "wb") as f:
                f.write(r.read())
        except Exception as e:
            print(f"  fetch failed: {e}", file=sys.stderr)
            continue
        digest = sha256(MAT_PATH)
        if digest == EXPECTED_SHA256:
            print(f"verified sha256 {digest} ({os.path.getsize(MAT_PATH)} bytes)")
            return MAT_PATH
        print(f"  hash mismatch ({digest}); trying next source", file=sys.stderr)
    sys.exit("ERROR: could not obtain a hash-verified copy of the scene from any source")


def load_cube(path):
    cube = np.asarray(sio.loadmat(path)["indian_pines_corrected"]).transpose(2, 0, 1)
    return np.ascontiguousarray(cube, dtype="<u2")


def load_lib():
    if not os.path.exists(LIB_PATH):
        print("libpfc.so not built; running make sharedlib ...", file=sys.stderr)
        subprocess.run(["make", "-C", FLIGHT, "sharedlib"], check=True)

    class Params(C.Structure):
        _fields_ = [("width", C.c_uint32), ("height", C.c_uint32), ("count", C.c_uint32),
                    ("bitdepth", C.c_uint8), ("elem", C.c_uint8), ("is_signed", C.c_uint8)]

    lib = C.CDLL(LIB_PATH)
    lib.pfc_workmem_bytes.restype = C.c_size_t
    lib.pfc_bound.restype = C.c_size_t
    lib.pfc_bound.argtypes = [C.c_int, C.c_size_t]
    for fn in ("pfc_encode", "pfc_decode"):
        getattr(lib, fn).restype = C.c_int
    lib.pfc_encode.argtypes = [C.c_int, C.POINTER(Params), C.c_void_p, C.c_size_t,
                               C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t), C.c_void_p]
    lib.pfc_decode.argtypes = [C.c_void_p, C.c_size_t, C.c_void_p, C.c_size_t,
                               C.POINTER(C.c_size_t), C.c_void_p]
    return lib, Params


def encode_spectral(lib, Params, cube, refresh, work):
    """Encode the cube at the given refresh interval, verify lossless round-trip."""
    Z, H, W = cube.shape
    n = cube.nbytes
    cap = lib.pfc_bound(PFC_CODEC_SPECTRAL, n)
    enc = (C.c_uint8 * cap)()
    out = C.c_size_t(0)
    p = Params(W, H, Z, 16, refresh, 0)
    st = lib.pfc_encode(PFC_CODEC_SPECTRAL, C.byref(p), cube.ctypes.data, n,
                        enc, cap, C.byref(out), work)
    assert st == 0, f"encode failed refresh={refresh} status={st}"
    dec = np.empty(Z * H * W, dtype="<u2")
    dout = C.c_size_t(0)
    st = lib.pfc_decode(enc, out.value, dec.ctypes.data, n, C.byref(dout), work)
    assert st == 0, f"decode failed refresh={refresh}"
    assert np.array_equal(dec.reshape(Z, H, W), cube), f"lossless failed refresh={refresh}"
    return out.value


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="compare against the documented §2.5.1 table and exit non-zero on drift")
    args = ap.parse_args()

    cube = load_cube(acquire())
    Z, H, W = cube.shape
    raw = cube.nbytes
    print(f"AVIRIS Indian Pines: {Z} bands {H}x{W} uint16, raw {raw / 1e6:.2f} MB")

    lib, Params = load_lib()
    work = (C.c_uint8 * lib.pfc_workmem_bytes())()

    rows = []
    baseline = None
    for n in INTERVALS:
        sz = encode_spectral(lib, Params, cube, n, work)
        if baseline is None:
            baseline = sz
        ratio = raw / sz
        cost = (sz / baseline - 1.0) * 100.0
        divmark = " *" if (n == 0 or Z % n == 0) else ""
        print(f"refresh={n:3d}: {ratio:.3f}x  sz={sz:8d}  cost={cost:+7.2f}%{divmark}")
        rows.append((n, ratio, cost))

    print("\n* = interval divides band count (200) evenly")

    if not args.check:
        return
    print("\n--check vs documented §2.5.1 table (mission-safety.md):")
    bad = 0
    for n, ratio, cost in rows:
        dratio, dcost = DOCUMENTED[n]
        ok = abs(ratio - dratio) <= RATIO_TOL and abs(cost - dcost) <= COST_TOL
        bad += not ok
        mark = "OK " if ok else "DRIFT"
        print(f"  refresh={n:3d}: measured {ratio:.3f}x/{cost:+.2f}%  "
              f"documented {dratio:.3f}x/{dcost:+.2f}%  [{mark}]")
    if bad:
        sys.exit(f"CHECK FAILED: {bad}/{len(rows)} rows drifted beyond noise "
                 f"(ratio ±{RATIO_TOL}, cost ±{COST_TOL}pp)")
    print("CHECK PASSED: all rows within noise of the documented table")


if __name__ == "__main__":
    main()
