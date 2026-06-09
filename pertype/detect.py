"""Identify what kind of data a byte string is, and which codec suits it best.

This is the unifying layer over the specialist codecs: like the ``file`` command, it
sniffs format (magic bytes first, then text-content heuristics) and reports a ``kind``
plus the ``codec`` that should compress it best. A dispatcher (CLI ``identify`` /
``auto``) uses this to route instead of making the user pick a subcommand.

The honest boundary: our big wins need *structure* (image dimensions, a trained text
model, …), so detection pays off most on self-describing formats (FITS / DICOM / WAV /
y4m / PNG / TIFF-raw / .npy). Opaque or already-compressed data falls back to the
generic codec or to storing as-is, and that's reported rather than hidden.
"""

# codec recommendations
IMAGE, VIDEO, AUDIO, ARRAY, TEXT, GENERIC, STORE = (
    "imagecodec", "videocodec", "audiocodec", "imagecodec", "model", "generic", "store")


class Detection:
    def __init__(self, kind, codec, detail=""):
        self.kind = kind          # e.g. "image/fits", "text/json"
        self.codec = codec        # which codec is ideal
        self.detail = detail

    def __repr__(self):
        return f"Detection(kind={self.kind!r}, codec={self.codec!r}, detail={self.detail!r})"


# (magic bytes at offset, kind, codec, detail). Offset is where to match.
_MAGIC = [
    (0, b"\x89PNG\r\n\x1a\n", "image/png", STORE, "PNG (already lossless-compressed)"),
    (0, b"\xff\xd8\xff", "image/jpeg", STORE, "JPEG (already compressed)"),
    (0, b"GIF8", "image/gif", STORE, "GIF (already compressed)"),
    (0, b"YUV4MPEG2", "video/y4m", VIDEO, "raw YUV video — motion-compensated codec"),
    (0, b"\x93NUMPY", "array/npy", ARRAY, "NumPy array — predict per dtype/shape"),
    (0, b"SIMPLE  =", "image/fits", IMAGE, "FITS scientific image — gray/volume codec"),
    (0, b"\x1f\x8b", "compressed/gzip", STORE, "gzip stream (already compressed)"),
    (0, b"PK\x03\x04", "compressed/zip", STORE, "zip archive (already compressed)"),
    (0, b"\xfd7zXZ\x00", "compressed/xz", STORE, "xz stream (already compressed)"),
    (0, b"\x28\xb5\x2f\xfd", "compressed/zstd", STORE, "zstd stream (already compressed)"),
    (0, b"BZh", "compressed/bzip2", STORE, "bzip2 stream (already compressed)"),
    (0, b"\x7fELF", "binary/elf", GENERIC, "ELF executable — generic LZ"),
    (0, b"%PDF", "doc/pdf", STORE, "PDF (largely pre-compressed streams)"),
    (128, b"DICM", "image/dicom", IMAGE, "DICOM medical image — gray/volume codec"),
]


def _is_tiff_raw(data):
    """TIFF byte order + magic 42; Canon CR2 adds a 'CR' marker at offset 8."""
    if data[:2] in (b"II", b"MM") and len(data) >= 4:
        le = data[:2] == b"II"
        magic = int.from_bytes(data[2:4], "little" if le else "big")
        if magic == 42:
            if len(data) >= 10 and data[8:10] == b"CR":
                return Detection("image/cr2", IMAGE, "Canon raw (CR2) — Bayer codec")
            return Detection("image/tiff", IMAGE, "TIFF raster — image codec")
    return None


def _is_wav(data):
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return Detection("audio/wav", AUDIO, "WAV PCM — predictive audio codec")
    return None


def _printable_ratio(sample):
    if not sample:
        return 0.0
    ok = sum(1 for b in sample if b in (9, 10, 13) or 32 <= b < 127)
    return ok / len(sample)


def _classify_text(data):
    """Sub-classify a text blob by cheap content cues."""
    head = data[:4096].lstrip()
    s = head.decode("utf-8", "replace")
    low = s.lower()
    if s[:1] in "{[":
        return "text/json"
    if s[:1] == "<":
        return "text/html" if "<!doctype html" in low or "<html" in low else "text/xml"
    # source code: keyword density
    kw = ("def ", "import ", "class ", "return ", "#include", "function ", "public ",
          "package ", "=>", "const ", "var ")
    if sum(low.count(k) for k in kw) >= 3:
        return "text/code"
    lines = [l for l in data[:8192].split(b"\n") if l][:6]
    # log: leading timestamp-ish tokens on most lines (checked before CSV — log lines
    # often contain commas too, but a leading timestamp is the stronger signal).
    import re
    ts = re.compile(rb"^\s*[\[\(]?\d{4}-\d\d-\d\d|^\s*[A-Z][a-z]{2}\s+\d+\s+\d\d:\d\d|^\s*\d\d:\d\d:\d\d")
    if len(lines) >= 3 and sum(bool(ts.match(l)) for l in lines) >= len(lines) // 2 + 1:
        return "text/log"
    # CSV: first lines have a consistent, plural comma count
    if len(lines) >= 3 and all(l.count(b",") == lines[0].count(b",") >= 1 for l in lines):
        return "text/csv"
    return "text/plain"


def identify(data, name=None):
    """Return a :class:`Detection` for ``data`` (bytes). ``name`` (a filename) is only
    a weak tiebreaker — content always wins."""
    if not data:
        return Detection("empty", STORE, "empty input")
    for off, magic, kind, codec, detail in _MAGIC:
        if data[off:off + len(magic)] == magic:
            return Detection(kind, codec, detail)
    for probe in (_is_tiff_raw, _is_wav):
        d = probe(data)
        if d is not None:
            return d
    # text vs binary by printable ratio of a sample
    sample = data[:8192]
    if _printable_ratio(sample) >= 0.90:
        kind = _classify_text(data)
        return Detection(kind, TEXT, f"text — best with a trained '{kind.split('/')[1]}' model")
    return Detection("binary/unknown", GENERIC, "no known structure — generic LZ codec")
