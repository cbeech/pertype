//! Lossless low-cardinality float codec — the Rust twin of `compressor/floatcodec.py`.
//! Maps each value's exact bit pattern to a dictionary index, delta-codes the smooth
//! index field (raw/delta/Δ²) under the ctxcoder, and deflates the dictionary; store
//! fallback. Round-trips and is cross-compatible with the Python version (the deflated
//! dictionary is a valid, cross-decodable zlib stream — not byte-identical to CPython's).

use crate::ctxcoder;
use crate::zlibw;

const FMAGIC: &[u8] = b"FLT1";
const M_STORE: u8 = 0;
const M_DICT: u8 = 1;

fn u32be(x: usize) -> [u8; 4] {
    (x as u32).to_be_bytes()
}
fn rd_u32(b: &[u8], p: usize) -> usize {
    u32::from_be_bytes([b[p], b[p + 1], b[p + 2], b[p + 3]]) as usize
}
fn read_le(body: &[u8], i: usize, itemsize: usize) -> u64 {
    let mut v = 0u64;
    for b in 0..itemsize {
        v |= (body[i * itemsize + b] as u64) << (8 * b);
    }
    v
}
fn write_le(out: &mut Vec<u8>, v: u64, itemsize: usize) {
    for b in 0..itemsize {
        out.push(((v >> (8 * b)) & 0xFF) as u8);
    }
}

pub fn encode(data: &[u8], itemsize: usize) -> Vec<u8> {
    let mut store = Vec::with_capacity(data.len() + 5);
    store.extend_from_slice(FMAGIC);
    store.push(M_STORE);
    store.extend_from_slice(data);
    if itemsize != 4 && itemsize != 8 {
        return store;
    }
    let n = data.len() / itemsize;
    if n < 8 {
        return store;
    }
    let body = &data[..n * itemsize];
    let trailing = &data[n * itemsize..];
    let bits: Vec<u64> = (0..n).map(|i| read_le(body, i, itemsize)).collect();
    let mut uniq = bits.clone();
    uniq.sort_unstable();
    uniq.dedup();
    let inv: Vec<i64> = bits.iter().map(|&v| uniq.binary_search(&v).unwrap() as i64).collect();
    let (sel, iblob) = ctxcoder::code_idx(&inv);
    let mut uniq_bytes = Vec::with_capacity(uniq.len() * itemsize);
    for &v in &uniq {
        write_le(&mut uniq_bytes, v, itemsize);
    }
    let dz = zlibw::deflate(&uniq_bytes);

    let mut out = Vec::new();
    out.extend_from_slice(FMAGIC);
    out.push(M_DICT);
    out.push(itemsize as u8);
    out.extend_from_slice(&u32be(n));
    out.extend_from_slice(&u32be(trailing.len()));
    out.extend_from_slice(trailing);
    out.extend_from_slice(&u32be(uniq.len()));
    out.extend_from_slice(&u32be(dz.len()));
    out.extend_from_slice(&dz);
    out.push(sel);
    out.extend_from_slice(&u32be(iblob.len()));
    out.extend_from_slice(&iblob);
    if out.len() < store.len() {
        out
    } else {
        store
    }
}

pub fn decode(blob: &[u8]) -> Vec<u8> {
    assert_eq!(&blob[..4], FMAGIC, "not a FLT1 stream");
    if blob[4] == M_STORE {
        return blob[5..].to_vec();
    }
    let itemsize = blob[5] as usize;
    let mut p = 6;
    let n = rd_u32(blob, p);
    p += 4;
    let tl = rd_u32(blob, p);
    p += 4;
    let trailing = &blob[p..p + tl];
    p += tl;
    let nu = rd_u32(blob, p);
    p += 4;
    let dl = rd_u32(blob, p);
    p += 4;
    let uniq_bytes = zlibw::inflate(&blob[p..p + dl]);
    p += dl;
    let uniq: Vec<u64> = (0..nu).map(|i| read_le(&uniq_bytes, i, itemsize)).collect();
    let sel = blob[p];
    p += 1;
    let il = rd_u32(blob, p);
    p += 4;
    let mut inv = ctxcoder::decode(&blob[p..p + il], n);
    for _ in 0..sel {
        let mut acc = 0i64;
        for v in inv.iter_mut() {
            acc += *v;
            *v = acc;
        }
    }
    let mut out = Vec::with_capacity(n * itemsize + tl);
    for &ix in &inv {
        write_le(&mut out, uniq[ix as usize], itemsize);
    }
    out.extend_from_slice(trailing);
    out
}

#[no_mangle]
pub unsafe extern "C" fn float_encode(data: *const u8, len: i64, itemsize: i64, out: *mut u8, cap: i64) -> i64 {
    let data = std::slice::from_raw_parts(data, len as usize);
    let blob = encode(data, itemsize as usize);
    if blob.len() as i64 > cap {
        return -1;
    }
    std::ptr::copy_nonoverlapping(blob.as_ptr(), out, blob.len());
    blob.len() as i64
}

#[no_mangle]
pub unsafe extern "C" fn float_decode(input: *const u8, len: i64, out: *mut u8, cap: i64) -> i64 {
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
    fn roundtrip() {
        // low-cardinality f32 grid (few distinct values)
        let mut data = Vec::new();
        let mut v = 0i32;
        let mut s: u64 = 3;
        for _ in 0..20000 {
            s = s.wrapping_mul(6364136223846793005).wrapping_add(1);
            v += (s >> 62) as i32 - 1;
            data.extend_from_slice(&((v as f32) / 100.0).to_le_bytes());
        }
        assert_eq!(decode(&encode(&data, 4)), data);
        // f64 incl NaN/-0.0/inf bit patterns survive
        let vals = [0.0f64, -0.0, f64::NAN, f64::INFINITY, 1.5, 1.5];
        let mut d8 = Vec::new();
        for i in 0..6000 {
            d8.extend_from_slice(&vals[i % vals.len()].to_le_bytes());
        }
        assert_eq!(decode(&encode(&d8, 8)), d8);
    }
}
