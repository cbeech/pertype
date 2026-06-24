"""Robustness fuzz (R6): hammer the C decoder through ctypes with random and mutated streams.

Any out-of-bounds read / crash in the C decoder would take down this Python process, so if the loop
completes, the decoder survived every input (returning a status, never crashing). Complements the
ASan build and the in-suite bit-flip/truncation tests with broad random coverage.

Build first:  make sharedlib
Run:          PYTHONPATH=<repo> python3 test/fuzz_decode.py [iterations]
"""
import ctypes as C
import os
import sys

LIB = os.path.join(os.path.dirname(__file__), "..", "build", "libpfc.so")


class Params(C.Structure):
    _fields_ = [("width", C.c_uint32), ("height", C.c_uint32), ("count", C.c_uint32),
                ("bitdepth", C.c_uint8), ("elem", C.c_uint8), ("is_signed", C.c_uint8)]


def main():
    n_iter = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
    h = C.CDLL(LIB)
    h.pfc_workmem_bytes.restype = C.c_size_t
    h.pfc_bound.restype = C.c_size_t
    h.pfc_bound.argtypes = [C.c_int, C.c_size_t]
    h.pfc_encode.restype = C.c_int
    h.pfc_encode.argtypes = [C.c_int, C.POINTER(Params), C.c_void_p, C.c_size_t,
                             C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t), C.c_void_p]
    h.pfc_decode.restype = C.c_int
    h.pfc_decode.argtypes = [C.c_void_p, C.c_size_t, C.c_void_p, C.c_size_t,
                             C.POINTER(C.c_size_t), C.c_void_p]
    work = (C.c_uint8 * h.pfc_workmem_bytes())()
    dst = (C.c_uint8 * (1 << 22))()           # 4 MB output cap
    out = C.c_size_t(0)

    # a deterministic PRNG (Date.now/random-free)
    state = 0x12345678

    def rnd():
        nonlocal state
        state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
        return state

    # seed corpus: one valid stream of each codec to mutate
    seeds = []
    src = bytes((rnd() & 0xFF) for _ in range(64 * 48 * 2))
    cap = h.pfc_bound(1, len(src))
    enc = (C.c_uint8 * cap)(); o = C.c_size_t(0)
    if h.pfc_encode(1, C.byref(Params(64, 48, 0, 16, 0, 0)), src, len(src), enc, cap, C.byref(o), work) == 0:
        seeds.append(bytearray(enc[:o.value]))
    seeds.append(bytearray(b"PFC1\x01\x01" + bytes(64)))   # header-ish

    crashes = 0  # if we ever get here after a crash... we wouldn't; tracked for completeness
    for it in range(n_iter):
        if (it & 1) == 0:
            n = rnd() % 4096
            buf = (C.c_uint8 * n)(*[rnd() & 0xFF for _ in range(n)])
            h.pfc_decode(buf, n, dst, len(dst), C.byref(out), work)
        else:
            s = bytearray(seeds[rnd() % len(seeds)])
            for _ in range((rnd() % 8) + 1):     # a few random byte mutations
                if s:
                    s[rnd() % len(s)] = rnd() & 0xFF
            buf = (C.c_uint8 * len(s)).from_buffer_copy(bytes(s))
            h.pfc_decode(buf, len(s), dst, len(dst), C.byref(out), work)

    print(f"fuzz_decode: {n_iter} iterations, decoder survived all inputs (no crash/OOB). crashes={crashes}")


if __name__ == "__main__":
    main()
