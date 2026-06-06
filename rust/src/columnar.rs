//! Columnar codec for fixed-width binary record streams — byte-identical to
//! `compressor/columnar.py`. De-interleaves records into per-field integer columns and
//! codes each as the smallest of raw / delta / second-difference under the ctxcoder; a
//! self-describing `COL1` container with a store fallback. A complete standalone codec:
//! a blob it produces decodes in the Python version and vice versa.

use crate::ctxcoder;

const CMAGIC: &[u8] = b"COL1";
const M_STORE: u8 = 0;
const M_COL: u8 = 1;
const ELEMS: [usize; 3] = [4, 2, 1]; // uniform-tiling field widths tried for an auto schema

fn u32be(x: usize) -> [u8; 4] {
    (x as u32).to_be_bytes()
}
fn u16be(x: usize) -> [u8; 2] {
    (x as u16).to_be_bytes()
}
fn rd_u32(b: &[u8], p: usize) -> usize {
    u32::from_be_bytes([b[p], b[p + 1], b[p + 2], b[p + 3]]) as usize
}
fn rd_u16(b: &[u8], p: usize) -> usize {
    u16::from_be_bytes([b[p], b[p + 1]]) as usize
}

fn deinterleave(body: &[u8], n: usize, schema: &[u8]) -> Vec<Vec<i64>> {
    let w: usize = schema.iter().map(|&x| x as usize).sum();
    let mut cols = Vec::with_capacity(schema.len());
    let mut off = 0usize;
    for &fw in schema {
        let fw = fw as usize;
        let mut c = vec![0i64; n];
        for r in 0..n {
            let mut v = 0i64;
            for b in 0..fw {
                v += (body[r * w + off + b] as i64) << (8 * b);
            }
            c[r] = v;
        }
        cols.push(c);
        off += fw;
    }
    cols
}

fn interleave(cols: &[Vec<i64>], n: usize, schema: &[u8]) -> Vec<u8> {
    let w: usize = schema.iter().map(|&x| x as usize).sum();
    let mut out = vec![0u8; n * w];
    let mut off = 0usize;
    for (c, &fw) in cols.iter().zip(schema) {
        let fw = fw as usize;
        for r in 0..n {
            for b in 0..fw {
                out[r * w + off + b] = ((c[r] >> (8 * b)) & 0xFF) as u8;
            }
        }
        off += fw;
    }
    out
}

fn try_schema(data: &[u8], schema: &[u8]) -> Option<Vec<u8>> {
    let w: usize = schema.iter().map(|&x| x as usize).sum();
    if w < 1 || schema.iter().any(|&x| x < 1 || x > 4) {
        return None;
    }
    let n = data.len() / w;
    if n < 2 {
        return None;
    }
    let body = &data[..n * w];
    let trailing = &data[n * w..];
    let mut out = Vec::new();
    out.extend_from_slice(CMAGIC);
    out.push(M_COL);
    out.push(schema.len() as u8);
    out.extend_from_slice(schema);
    out.extend_from_slice(&u32be(n));
    out.extend_from_slice(&u16be(trailing.len()));
    out.extend_from_slice(trailing);
    for col in deinterleave(body, n, schema) {
        let (sel, blob) = ctxcoder::code_idx(&col);
        out.push(sel);
        out.extend_from_slice(&u32be(blob.len()));
        out.extend_from_slice(&blob);
    }
    Some(out)
}

/// Byte autocorrelation: dominant period, or 0 if no clear periodicity.
pub fn detect_width(data: &[u8]) -> usize {
    let sample = &data[..data.len().min(1 << 16)];
    if sample.len() < 64 {
        return 0;
    }
    let hi = 256.min(sample.len() / 4);
    let (mut best_p, mut best) = (0usize, 0.0f64);
    for p in 2..=hi {
        let mut m = 0usize;
        for i in 0..sample.len() - p {
            if sample[i] == sample[i + p] {
                m += 1;
            }
        }
        let frac = m as f64 / (sample.len() - p) as f64;
        if frac > best {
            best = frac;
            best_p = p;
        }
    }
    if best > 0.2 {
        best_p
    } else {
        0
    }
}

/// `width` 0 = auto-detect the record period; >0 = search uniform tilings of that width.
pub fn encode(data: &[u8], width: usize) -> Vec<u8> {
    let w = if width != 0 { width } else { detect_width(data) };
    let mut best: Option<Vec<u8>> = None;
    if w >= 2 {
        for &e in &ELEMS {
            if w % e == 0 {
                let schema = vec![e as u8; w / e];
                if let Some(blob) = try_schema(data, &schema) {
                    if best.as_ref().map_or(true, |b| blob.len() < b.len()) {
                        best = Some(blob);
                    }
                }
            }
        }
    }
    let mut store = Vec::with_capacity(data.len() + 5);
    store.extend_from_slice(CMAGIC);
    store.push(M_STORE);
    store.extend_from_slice(data);
    match best {
        Some(b) if b.len() < store.len() => b,
        _ => store,
    }
}

/// Encode with an explicit field schema (e.g. parsed from a LAS header).
pub fn encode_schema(data: &[u8], schema: &[u8]) -> Vec<u8> {
    let mut store = Vec::with_capacity(data.len() + 5);
    store.extend_from_slice(CMAGIC);
    store.push(M_STORE);
    store.extend_from_slice(data);
    match try_schema(data, schema) {
        Some(b) if b.len() < store.len() => b,
        _ => store,
    }
}

pub fn decode(blob: &[u8]) -> Vec<u8> {
    assert_eq!(&blob[..4], CMAGIC, "not a COL1 stream");
    if blob[4] == M_STORE {
        return blob[5..].to_vec();
    }
    let mut p = 5;
    let nf = blob[p] as usize;
    p += 1;
    let schema = blob[p..p + nf].to_vec();
    p += nf;
    let n = rd_u32(blob, p);
    p += 4;
    let tl = rd_u16(blob, p);
    p += 2;
    let trailing = &blob[p..p + tl];
    p += tl;
    let mut cols = Vec::with_capacity(nf);
    for _ in 0..nf {
        let sel = blob[p];
        p += 1;
        let ln = rd_u32(blob, p);
        p += 4;
        let mut vals = ctxcoder::decode(&blob[p..p + ln], n);
        p += ln;
        for _ in 0..sel {
            // undo 0/1/2 cumulative sums
            let mut acc = 0i64;
            for v in vals.iter_mut() {
                acc += *v;
                *v = acc;
            }
        }
        cols.push(vals);
    }
    let mut out = interleave(&cols, n, &schema);
    out.extend_from_slice(trailing);
    out
}

#[no_mangle]
pub unsafe extern "C" fn columnar_encode(
    data: *const u8, len: i64, width: i64, out: *mut u8, cap: i64,
) -> i64 {
    let data = std::slice::from_raw_parts(data, len as usize);
    let blob = encode(data, width as usize);
    if blob.len() as i64 > cap {
        return -1;
    }
    std::ptr::copy_nonoverlapping(blob.as_ptr(), out, blob.len());
    blob.len() as i64
}

#[no_mangle]
pub unsafe extern "C" fn columnar_decode(input: *const u8, len: i64, out: *mut u8, cap: i64) -> i64 {
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
        // 3 int32 columns (smooth) + 1 u16, interleaved into 14-byte records
        let n = 3000usize;
        let mut data = vec![0u8; n * 14];
        let (mut x, mut y, mut z) = (0i64, 0i64, 0i64);
        let mut s: u64 = 7;
        for r in 0..n {
            s = s.wrapping_mul(6364136223846793005).wrapping_add(1);
            x += (s >> 62) as i64 - 1;
            y += ((s >> 60) & 3) as i64 - 1;
            z += ((s >> 58) & 1) as i64;
            for (off, v, wd) in [(0, x, 4), (4, y, 4), (8, z, 4), (12, (r as i64) & 0xFFFF, 2)] {
                for b in 0..wd {
                    data[r * 14 + off + b] = ((v >> (8 * b)) & 0xFF) as u8;
                }
            }
        }
        assert_eq!(decode(&encode_schema(&data, &[4, 4, 4, 2])), data);
        assert_eq!(decode(&encode(&data, 14)), data);
        // high-entropy -> store, never expands
        let rnd: Vec<u8> = (0..5000u32).map(|i| (i.wrapping_mul(2654435761) >> 24) as u8).collect();
        assert_eq!(decode(&encode(&rnd, 0)), rnd);
    }
}
