"""Parse / serialize the YUV4MPEG2 (.y4m) container, byte-exact.

Shared by the CLI (`video-encode`) and the auto front door so a `.y4m` round-trips
exactly: the stream header line and every per-frame ``FRAME...\\n`` header are preserved
verbatim, and the planes are returned as numpy arrays the video codec can compress.
Supports 4:2:0 / 4:2:2 / 4:4:4 / mono.
"""
import numpy as np


def chroma_div(ctag):
    """(W-divisor, H-divisor) for a y4m C-tag, or None for monochrome."""
    body = ctag[1:]                                  # drop leading 'C'
    if body.startswith("mono") or body.startswith("400"):
        return None
    for k, div in (("420", (2, 2)), ("411", (4, 1)), ("422", (2, 1)), ("444", (1, 1))):
        if body.startswith(k):
            return div
    raise ValueError(f"unsupported y4m colour space {ctag}")


def parse(raw):
    """Parse .y4m bytes -> (header_line, [frame_headers], [planes]). ``planes`` is
    [Y] (mono) or [Y, U, V]; each plane is a (frames, h, w) uint8 array."""
    nl = raw.index(b"\n")
    header = raw[:nl + 1]
    W = H = None
    ctag = "C420"
    for tok in raw[:nl].decode("ascii").split():
        if tok[0] == "W":
            W = int(tok[1:])
        elif tok[0] == "H":
            H = int(tok[1:])
        elif tok[0] == "C":
            ctag = tok
    if W is None or H is None:
        raise ValueError("malformed .y4m header")
    div = chroma_div(ctag)
    ys = W * H
    cw, ch = (W // div[0], H // div[1]) if div else (0, 0)
    cs = cw * ch
    fheaders, Ys, Us, Vs = [], [], [], []
    pos = nl + 1
    while pos < len(raw):
        fnl = raw.index(b"\n", pos)                  # this frame's "FRAME...\n", verbatim
        fheaders.append(raw[pos:fnl + 1])
        pos = fnl + 1
        Ys.append(np.frombuffer(raw[pos:pos + ys], np.uint8).reshape(H, W)); pos += ys
        if div:
            Us.append(np.frombuffer(raw[pos:pos + cs], np.uint8).reshape(ch, cw)); pos += cs
            Vs.append(np.frombuffer(raw[pos:pos + cs], np.uint8).reshape(ch, cw)); pos += cs
    planes = [np.stack(Ys)] + ([np.stack(Us), np.stack(Vs)] if div else [])
    return header, fheaders, planes


def serialize(header, fheaders, planes):
    """Inverse of :func:`parse` — reproduce the exact .y4m bytes."""
    out = bytearray(header)
    for t in range(len(planes[0])):
        out += fheaders[t]
        for p in planes:
            out += p[t].tobytes()
    return bytes(out)
