//! Thin zlib wrapper (flate2). Output is a valid zlib stream that Python's `zlib` decodes
//! and vice versa, so the float/CSV codecs are fully cross-compatible — though the exact
//! deflate bytes differ from CPython's zlib (different deflate implementation), so those
//! sub-blobs are *not* byte-identical (unlike the arithmetic-coded parts).

use std::io::{Read, Write};

use flate2::read::ZlibDecoder;
use flate2::write::ZlibEncoder;
use flate2::Compression;

pub fn deflate(data: &[u8]) -> Vec<u8> {
    let mut e = ZlibEncoder::new(Vec::new(), Compression::new(9));
    e.write_all(data).unwrap();
    e.finish().unwrap()
}

pub fn inflate(data: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    ZlibDecoder::new(data).read_to_end(&mut out).unwrap();
    out
}
