//! Thin zlib wrapper (flate2). Output is a valid zlib stream that Python's `zlib` decodes
//! and vice versa, so the float/CSV codecs are fully cross-compatible — though the exact
//! deflate bytes differ from CPython's zlib (different deflate implementation), so those
//! sub-blobs are *not* byte-identical (unlike the arithmetic-coded parts).

use std::io::{Read, Write};

use flate2::read::ZlibDecoder;
use flate2::write::ZlibEncoder;
use flate2::Compression;

pub fn deflate(data: &[u8]) -> Vec<u8> {
    deflate_level(data, 9)
}

pub fn deflate_level(data: &[u8], level: u32) -> Vec<u8> {
    let mut e = ZlibEncoder::new(Vec::new(), Compression::new(level));
    e.write_all(data).unwrap();
    e.finish().unwrap()
}

/// Compressed size at `level` — used by the training transform selector (proxy ranking).
#[no_mangle]
pub unsafe extern "C" fn zlib_size(data: *const u8, len: i64, level: i64) -> i64 {
    deflate_level(std::slice::from_raw_parts(data, len as usize), level as u32).len() as i64
}

pub fn inflate(data: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    ZlibDecoder::new(data).read_to_end(&mut out).unwrap();
    out
}
