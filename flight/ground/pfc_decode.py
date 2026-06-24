"""Independent pure-Python ground decoder for the PFC1 wire format.

This is a SECOND, dependency-light implementation of the decode side (no ctypes, no C). Its purpose
is twofold (R7): prove that a downlinked PFC1 stream is decodable by an independent implementation
on the ground — NASA need not trust/port the flight C — and serve as the bit-exact oracle that the
C encoder is validated against.

Mirrors flight/src exactly: 32-bit carryless range coder, adaptive category model, MED image /
delta seq / byte-plane columnar front-ends. Integer-only; uint32 wrapping is emulated with masks.
"""
import struct

MASK = 0xFFFFFFFF
TOP = 1 << 24
BOT = 1 << 16
KMAX, NSYM, NCTX = 32, 33, 20
INC, MODEL_MAX = 24, 1 << 13
HDR, BLKHDR = 20, 9
RAW = 1

IMAGE, SEQ, FLOAT, COLUMNAR = 1, 2, 3, 4


def _bitlen(u):
    k = 0
    while u:
        k += 1
        u >>= 1
    return k


def _zigzag(n):                       # signed int32 -> uint32
    u = n & MASK
    return ((u << 1) & MASK) ^ (MASK if n < 0 else 0)


def _unzigzag(u):                     # uint32 -> signed int32
    n = (u >> 1) ^ ((0 - (u & 1)) & MASK)
    return n - (1 << 32) if n >= (1 << 31) else n


def _cat(resid):
    return min(_bitlen(_zigzag(resid)), NCTX - 1)


class _RC:
    """Range decoder mirroring pfc_arith.c."""
    def __init__(self, buf):
        self.buf = buf
        self.p = 0
        self.low = 0
        self.range = MASK
        self.code = 0
        for _ in range(4):
            self.code = ((self.code << 8) | self._byte()) & MASK

    def _byte(self):
        b = self.buf[self.p] if self.p < len(self.buf) else 0
        self.p += 1
        return b

    def getfreq(self, tot):
        self.range //= tot
        return ((self.code - self.low) & MASK) // self.range

    def _renorm(self):
        while True:
            if ((self.low ^ ((self.low + self.range) & MASK)) & MASK) < TOP:
                pass
            elif self.range < BOT:
                self.range = (-self.low) & (BOT - 1)
            else:
                break
            self.code = ((self.code << 8) | self._byte()) & MASK
            self.low = (self.low << 8) & MASK
            self.range = (self.range << 8) & MASK

    def update(self, cum, freq):
        self.low = (self.low + cum * self.range) & MASK
        self.range = (self.range * freq) & MASK
        self._renorm()

    def bits(self, nbits):
        v = 0
        for _ in range(nbits):
            bit = self.getfreq(2)
            self.update(bit, 1)
            v = (v << 1) | bit
        return v


class _Model:
    """Adaptive category model mirroring pfc_model.c."""
    def __init__(self):
        self.reset()

    def reset(self):
        self.freq = [[1] * NSYM for _ in range(NCTX)]
        self.tot = [NSYM] * NCTX

    def _update(self, ctx, k):
        self.freq[ctx][k] += INC
        self.tot[ctx] += INC
        if self.tot[ctx] >= MODEL_MAX:
            t = 0
            row = self.freq[ctx]
            for s in range(NSYM):
                row[s] = (row[s] + 1) >> 1
                t += row[s]
            self.tot[ctx] = t

    def decode(self, rc, ctx):
        target = rc.getfreq(self.tot[ctx])
        cum = 0
        k = 0
        row = self.freq[ctx]
        while k < KMAX and cum + row[k] <= target:
            cum += row[k]
            k += 1
        rc.update(cum, row[k])
        if k == 0:
            u = 0
        elif k == 1:
            u = 1
        else:
            u = (1 << (k - 1)) | rc.bits(k - 1)
        self._update(ctx, k)
        return _unzigzag(u)


def _crc32(buf):
    crc = 0xFFFFFFFF
    for byte in buf:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ (0xEDB88320 & (-(crc & 1) & MASK))
    return (~crc) & MASK


def _blocks(stream):
    """Yield (flags, payload) per block record after the header; verify CRC."""
    pos = HDR
    n = len(stream)
    while pos + BLKHDR <= n:
        plen, flags, crc = struct.unpack_from("<IBI", stream, pos)
        payload = stream[pos + BLKHDR:pos + BLKHDR + plen]
        if pos + BLKHDR + plen > n:
            raise ValueError("truncated block")
        if _crc32(payload) != crc:
            raise ValueError("CRC mismatch")
        yield flags, payload
        pos += BLKHDR + plen


# ----------------------------------------------------------------- image ----

def _med(px, w, x, y, y0, mid):
    if y == y0 and x == 0:
        return mid
    if y == y0:
        return px[y * w + x - 1]
    if x == 0:
        return px[(y - 1) * w + x]
    a, b, c = px[y * w + x - 1], px[(y - 1) * w + x], px[(y - 1) * w + x - 1]
    lo, hi = (a, b) if a < b else (b, a)
    if c >= hi:
        return lo
    if c <= lo:
        return hi
    return a + b - c


def _grad_ctx(px, w, x, y, y0):
    if y == y0 or x == 0:
        return 0
    a, b, c = px[y * w + x - 1], px[(y - 1) * w + x], px[(y - 1) * w + x - 1]
    g = abs(a - c) + abs(b - c)
    return min(_bitlen(g), NCTX - 1)


def _decode_image(stream):
    bd = stream[6]
    w, h, band = struct.unpack_from("<III", stream, 8)
    es = 2 if bd > 8 else 1
    mid = 1 << (bd - 1)
    px = [0] * (w * h)
    blocks = _blocks(stream)
    y0 = 0
    while y0 < h:
        y1 = min(y0 + band, h)
        flags, payload = next(blocks)
        if flags & RAW:
            for i in range(y1 - y0):
                for x in range(w):
                    off = (i * w + x) * es
                    px[(y0 + i) * w + x] = int.from_bytes(payload[off:off + es], "little")
        else:
            rc, m = _RC(payload), _Model()
            for y in range(y0, y1):
                for x in range(w):
                    pred = _med(px, w, x, y, y0, mid)
                    resid = m.decode(rc, _grad_ctx(px, w, x, y, y0))
                    px[y * w + x] = (pred + resid) & ((1 << (bd if bd > 8 else 8)) - 1)
        y0 = y1
    return b"".join(int(v).to_bytes(es, "little") for v in px)


# ----------------------------------------------------------------- seq ----

def _decode_seq(stream):
    elem, is_signed = stream[6], stream[7]
    count, block = struct.unpack_from("<II", stream, 8)
    out = [0] * count
    blocks = _blocks(stream)
    i0 = 0
    while i0 < count:
        n = min(block, count - i0)
        flags, payload = next(blocks)
        if flags & RAW:
            for i in range(n):
                out[i0 + i] = int.from_bytes(payload[i * elem:(i + 1) * elem], "little")
        else:
            rc, m = _RC(payload), _Model()
            prev, ctx = 0, 0
            for i in range(n):
                resid = m.decode(rc, ctx)
                cur = (prev + resid) & MASK
                out[i0 + i] = cur & ((1 << (8 * elem)) - 1)
                ctx = _cat(resid)
                prev = cur
        i0 += n
    return b"".join(int(v & ((1 << (8 * elem)) - 1)).to_bytes(elem, "little") for v in out)


# ----------------------------------------------------------------- columnar/float ----

def _decode_columnar(stream):
    do_delta = stream[6]
    rw, cnt, block_recs = struct.unpack_from("<III", stream, 8)
    out = bytearray(rw * cnt)
    blocks = _blocks(stream)
    r0 = 0
    while r0 < cnt:
        nr = min(block_recs, cnt - r0)
        flags, payload = next(blocks)
        if flags & RAW:
            out[r0 * rw:r0 * rw + nr * rw] = payload
        else:
            rc = _RC(payload)
            plane = [0] * (rw * nr)
            for c in range(rw):
                m = _Model()
                prev, ctx = 0, 0
                for r in range(nr):
                    resid = m.decode(rc, ctx)
                    cur = ((prev + resid) & 0xFF) if do_delta else (resid & 0xFF)
                    plane[c * nr + r] = cur
                    ctx = _cat(resid)
                    prev = cur
            for c in range(rw):
                for r in range(nr):
                    out[(r0 + r) * rw + c] = plane[c * nr + r]
        r0 += nr
    return bytes(out)


def decode(stream):
    """Decode a PFC1 byte stream to the original sample bytes (x86-native little-endian layout)."""
    if stream[:4] != b"PFC1":
        raise ValueError("not a PFC1 stream")
    if stream[4] != 1:
        raise ValueError("unsupported version")
    codec = stream[5]
    if codec == IMAGE:
        return _decode_image(stream)
    if codec == SEQ:
        return _decode_seq(stream)
    if codec in (FLOAT, COLUMNAR):
        return _decode_columnar(stream)
    raise ValueError(f"unsupported codec {codec}")
