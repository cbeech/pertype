//! Auto front door — tries the Rust codecs, verifies each round-trips, keeps the smallest.
//! Produces the **same `AZ` container as `pertype/auto.py`** for the methods Rust
//! implements (store / deflate / csv→columnar / binary→columnar), so a Rust-produced `.az`
//! is decoded by the Python `auto_decompress` and vice versa. (Python additionally routes
//! image/float/video/audio formats Rust doesn't carry; those methods aren't produced here.)

use crate::columnar;
use crate::csvcolumnar;
use crate::zlibw;

const AMAGIC: [u8; 2] = [b'A', b'Z'];
const AVERSION: u8 = 1;
const M_STORE: u8 = 0;
const M_ZLIB: u8 = 1;
const M_CSV: u8 = 4;
const M_COL: u8 = 5;

fn wrap(method: u8, payload: Vec<u8>) -> Vec<u8> {
    let mut out = vec![AMAGIC[0], AMAGIC[1], AVERSION, method];
    out.extend_from_slice(&payload);
    out
}

pub fn encode(data: &[u8]) -> Vec<u8> {
    let candidates = vec![
        wrap(M_STORE, data.to_vec()),
        wrap(M_ZLIB, zlibw::deflate(data)),
        wrap(M_CSV, csvcolumnar::encode(data)),
        wrap(M_COL, columnar::encode(data, 0)),
    ];
    let mut best: Option<Vec<u8>> = None;
    for c in candidates {
        if decode(&c) == data {                       // verify byte-exact before trusting it
            if best.as_ref().map_or(true, |b| c.len() < b.len()) {
                best = Some(c);
            }
        }
    }
    best.unwrap() // store always verifies
}

pub fn decode(blob: &[u8]) -> Vec<u8> {
    assert!(blob[..2] == AMAGIC && blob[2] == AVERSION, "not an AZ container");
    match blob[3] {
        M_STORE => blob[4..].to_vec(),
        M_ZLIB => zlibw::inflate(&blob[4..]),
        M_CSV => csvcolumnar::decode(&blob[4..]),
        M_COL => columnar::decode(&blob[4..]),
        m => panic!("AZ method {m} not supported by the Rust decoder"),
    }
}

pub fn method_name(blob: &[u8]) -> &'static str {
    match blob[3] {
        M_STORE => "store",
        M_ZLIB => "deflate",
        M_CSV => "csv->columnar",
        M_COL => "binary->columnar",
        _ => "?",
    }
}

#[no_mangle]
pub unsafe extern "C" fn auto_encode(data: *const u8, len: i64, out: *mut u8, cap: i64) -> i64 {
    let data = std::slice::from_raw_parts(data, len as usize);
    let blob = encode(data);
    if blob.len() as i64 > cap {
        return -1;
    }
    std::ptr::copy_nonoverlapping(blob.as_ptr(), out, blob.len());
    blob.len() as i64
}

#[no_mangle]
pub unsafe extern "C" fn auto_decode(input: *const u8, len: i64, out: *mut u8, cap: i64) -> i64 {
    let inp = std::slice::from_raw_parts(input, len as usize);
    let data = decode(inp);
    if data.len() as i64 > cap {
        return -1;
    }
    std::ptr::copy_nonoverlapping(data.as_ptr(), out, data.len());
    data.len() as i64
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn roundtrip_and_routes() {
        // CSV grid -> csv
        let mut s = String::from("a;b;n\n");
        for i in 0..1500 {
            s.push_str(&format!("2024-01-01;{}.{:02};{}\n", i / 100, i % 100, i));
        }
        let csv = s.into_bytes();
        let b = encode(&csv);
        assert_eq!(decode(&b), csv);
        assert_eq!(method_name(&b), "csv->columnar");
        // high-entropy -> store, never expands
        let rnd: Vec<u8> = (0..4000u32).map(|i| (i.wrapping_mul(2654435761) >> 24) as u8).collect();
        let br = encode(&rnd);
        assert_eq!(decode(&br), rnd);
        assert!(br.len() <= rnd.len() + 8);
    }
}
