"""Tests for the format detector (the 'file'-like identification layer)."""
from compressor.detect import identify


def test_magic_formats():
    assert identify(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20).kind == "image/png"
    assert identify(b"YUV4MPEG2 W64 H48").kind == "video/y4m"
    assert identify(b"SIMPLE  =                    T").kind == "image/fits"
    assert identify(b"\x93NUMPY\x01\x00").kind == "array/npy"
    assert identify(b"RIFF\x00\x00\x00\x00WAVEfmt ").codec == "audiocodec"
    assert identify(b"\x00" * 128 + b"DICM").kind == "image/dicom"
    assert identify(b"\xfd7zXZ\x00").codec == "store"      # already compressed


def test_tiff_and_cr2():
    assert identify(b"II*\x00" + b"\x00" * 4 + b"CR\x02\x00").kind == "image/cr2"
    assert identify(b"II*\x00" + b"\x08\x00\x00\x00").kind == "image/tiff"


def test_text_subtypes():
    assert identify(b'{"a": 1, "b": [2, 3]}\n' * 5).kind == "text/json"
    assert identify(b"<!DOCTYPE html><html><body>hi</body></html>").kind == "text/html"
    assert identify(b"<root><item>1</item></root>\n" * 5).kind == "text/xml"
    code = b"import os\ndef f(x):\n    return x + 1\nclass A: pass\n" * 3
    assert identify(code).kind == "text/code"
    log = b"2026-01-07 09:42:59,313 mod:50 message here\n" * 5
    assert identify(log).kind == "text/log"
    csv = b"a,b,c,d\n1,2,3,4\n5,6,7,8\n9,10,11,12\n"
    assert identify(csv).kind == "text/csv"


def test_binary_and_empty():
    assert identify(b"").codec == "store"
    assert identify(bytes(range(256)) * 4).kind == "binary/unknown"  # high-entropy bytes
