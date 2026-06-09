//! Columnar codec for regular delimited-text tables — the Rust twin of
//! `pertype/csvcolumnar.py`. Detects a regular grid, transposes to column-major, and
//! codes each column as numeric (scaled-int Δ), text-dictionary, or deflate — whichever is
//! smallest. Round-trips and is cross-compatible with the Python version (deflate sub-blobs
//! are valid cross-decodable zlib, not byte-identical to CPython's).

use std::collections::HashMap;

use rayon::prelude::*;

use crate::ctxcoder;
use crate::zlibw;

const CMAGIC: &[u8] = b"CSV1";
const M_STORE: u8 = 0;
const M_DEFLATE: u8 = 1;
const M_GRID: u8 = 2;
const DELIMS: [u8; 4] = [b';', b',', b'\t', b'|'];

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

fn split_bytes<'a>(data: &'a [u8], sep: &[u8]) -> Vec<&'a [u8]> {
    let mut out = Vec::new();
    let (mut start, mut i) = (0usize, 0usize);
    while i + sep.len() <= data.len() {
        if &data[i..i + sep.len()] == sep {
            out.push(&data[start..i]);
            i += sep.len();
            start = i;
        } else {
            i += 1;
        }
    }
    out.push(&data[start..]);
    out
}

fn count_sub(data: &[u8], sep: &[u8]) -> usize {
    let (mut c, mut i) = (0usize, 0usize);
    while i + sep.len() <= data.len() {
        if &data[i..i + sep.len()] == sep {
            c += 1;
            i += sep.len();
        } else {
            i += 1;
        }
    }
    c
}

fn join_bytes(parts: &[&[u8]], sep: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    for (i, p) in parts.iter().enumerate() {
        if i > 0 {
            out.extend_from_slice(sep);
        }
        out.extend_from_slice(p);
    }
    out
}

// --- numeric columns (canonical scaled-int) ----------------------------------

fn fmt(v: i64, ndec: usize) -> Vec<u8> {
    let neg = v < 0;
    let a = v.unsigned_abs();
    let body = if ndec == 0 {
        a.to_string()
    } else {
        let scale = 10u64.pow(ndec as u32);
        format!("{}.{:0width$}", a / scale, a % scale, width = ndec)
    };
    let mut s = String::new();
    if neg {
        s.push('-');
    }
    s.push_str(&body);
    s.into_bytes()
}

fn parse_uint(b: &[u8]) -> Option<i64> {
    let mut v: i64 = 0;
    for &c in b {
        v = v.checked_mul(10)?.checked_add((c - b'0') as i64)?;
    }
    Some(v)
}

fn parse_numeric(col: &[&[u8]]) -> Option<(usize, Vec<i64>)> {
    let first = col[0];
    if first.is_empty() {
        return None;
    }
    let ndec = match first.iter().position(|&b| b == b'.') {
        Some(dot) => first.len() - dot - 1,
        None => 0,
    };
    let scale = 10i64.checked_pow(ndec as u32)?;
    let mut vals = Vec::with_capacity(col.len());
    for &cell in col {
        let neg = cell.first() == Some(&b'-');
        let body = if neg { &cell[1..] } else { cell };
        let v: i64 = if ndec == 0 {
            if body.is_empty() || !body.iter().all(|b| b.is_ascii_digit()) {
                return None;
            }
            parse_uint(body)?
        } else {
            let dot = body.iter().position(|&b| b == b'.')?;
            let (ip, fp) = (&body[..dot], &body[dot + 1..]);
            if fp.len() != ndec
                || ip.is_empty()
                || !ip.iter().all(|b| b.is_ascii_digit())
                || !fp.iter().all(|b| b.is_ascii_digit())
            {
                return None;
            }
            parse_uint(ip)?.checked_mul(scale)?.checked_add(parse_uint(fp)?)?
        };
        let v = if neg { -v } else { v };
        let bl = if v == 0 { 0 } else { 64 - v.unsigned_abs().leading_zeros() };
        if bl > 62 || fmt(v, ndec) != cell {
            return None;
        }
        vals.push(v);
    }
    Some((ndec, vals))
}

// --- per-column coding -------------------------------------------------------

fn text_dict_block(col: &[&[u8]], delim: u8) -> Option<Vec<u8>> {
    let mut seen: HashMap<&[u8], usize> = HashMap::new();
    let mut distinct: Vec<&[u8]> = Vec::new();
    let mut inv: Vec<i64> = Vec::with_capacity(col.len());
    for &c in col {
        let j = *seen.entry(c).or_insert_with(|| {
            distinct.push(c);
            distinct.len() - 1
        });
        inv.push(j as i64);
    }
    if distinct.len() >= col.len() {
        return None;
    }
    let dz = zlibw::deflate(&join_bytes(&distinct, &[delim]));
    let (sel, iblob) = ctxcoder::code_idx(&inv);
    let mut out = vec![2u8];
    out.extend_from_slice(&u32be(distinct.len()));
    out.extend_from_slice(&u32be(dz.len()));
    out.extend_from_slice(&dz);
    out.push(sel);
    out.extend_from_slice(&u32be(iblob.len()));
    out.extend_from_slice(&iblob);
    Some(out)
}

fn encode_col(col: &[&[u8]], delim: u8) -> Vec<u8> {
    let text = zlibw::deflate(&join_bytes(col, &[delim]));
    let mut cands: Vec<Vec<u8>> = Vec::new();
    let mut t = vec![0u8];
    t.extend_from_slice(&u32be(text.len()));
    t.extend_from_slice(&text);
    cands.push(t);
    if let Some(blk) = text_dict_block(col, delim) {
        cands.push(blk);
    }
    if let Some((ndec, vals)) = parse_numeric(col) {
        let (sel, blob) = ctxcoder::code_idx(&vals);
        let mut nb = vec![1u8, ndec as u8, sel];
        nb.extend_from_slice(&u32be(blob.len()));
        nb.extend_from_slice(&blob);
        cands.push(nb);
    }
    let mut best = cands.remove(0);
    for c in cands {
        if c.len() < best.len() {
            best = c;
        }
    }
    best
}

// --- grid detection / encode -------------------------------------------------

struct Grid<'a> {
    delim: u8,
    lt: &'static [u8],
    has_tl: bool,
    header: &'a [u8],
    rows: Vec<&'a [u8]>,
}

fn detect_grid(data: &[u8]) -> Option<Grid<'_>> {
    let nl = count_sub(data, b"\n");
    if nl < 3 {
        return None;
    }
    let lt: &'static [u8] = if count_sub(data, b"\r\n") == nl { b"\r\n" } else { b"\n" };
    let has_tl = data.ends_with(lt);
    let mut lines = split_bytes(data, lt);
    if has_tl {
        lines.pop();
    }
    if lines.len() < 3 {
        return None;
    }
    let mut delim = None;
    for &d in &DELIMS {
        let k = count_sub(lines[0], &[d]);
        if k >= 1 && lines.iter().all(|ln| count_sub(ln, &[d]) == k) {
            delim = Some(d);
            break;
        }
    }
    let delim = delim?;
    let header = lines[0];
    let rows = lines[1..].to_vec();
    Some(Grid { delim, lt, has_tl, header, rows })
}

fn encode_grid(data: &[u8]) -> Option<Vec<u8>> {
    let g = detect_grid(data)?;
    let n = g.rows.len();
    if n < 2 {
        return None;
    }
    let k = count_sub(g.header, &[g.delim]) + 1;
    let split: Vec<Vec<&[u8]>> = g.rows.iter().map(|r| split_bytes(r, &[g.delim])).collect();
    let mut out = Vec::new();
    out.extend_from_slice(CMAGIC);
    out.push(M_GRID);
    out.push(g.delim);
    out.push(if g.lt == b"\n" { 0 } else { 1 });
    out.push(if g.has_tl { 1 } else { 0 });
    out.extend_from_slice(&u32be(n));
    out.extend_from_slice(&u16be(k));
    out.extend_from_slice(&u32be(g.header.len()));
    out.extend_from_slice(g.header);
    // independent columns -> encode in parallel; order preserved, so bytes are identical.
    let coded: Vec<Vec<u8>> = (0..k)
        .into_par_iter()
        .map(|c| {
            let col: Vec<&[u8]> = split.iter().map(|row| row[c]).collect();
            encode_col(&col, g.delim)
        })
        .collect();
    for blk in coded {
        out.extend_from_slice(&blk);
    }
    Some(out)
}

pub fn encode(data: &[u8]) -> Vec<u8> {
    let mut store = Vec::with_capacity(data.len() + 5);
    store.extend_from_slice(CMAGIC);
    store.push(M_STORE);
    store.extend_from_slice(data);
    let mut deflate = Vec::new();
    deflate.extend_from_slice(CMAGIC);
    deflate.push(M_DEFLATE);
    deflate.extend_from_slice(&zlibw::deflate(data));
    let mut best = if deflate.len() < store.len() { deflate } else { store };
    if let Some(grid) = encode_grid(data) {
        if grid.len() < best.len() && decode(&grid) == data {
            best = grid;
        }
    }
    best
}

// --- decode ------------------------------------------------------------------

fn decode_col(blob: &[u8], mut p: usize, n: usize, delim: u8) -> (Vec<Vec<u8>>, usize) {
    let kind = blob[p];
    p += 1;
    if kind == 0 {
        let ln = rd_u32(blob, p);
        p += 4;
        let cells = split_bytes(&zlibw::inflate(&blob[p..p + ln]), &[delim])
            .iter()
            .map(|c| c.to_vec())
            .collect();
        return (cells, p + ln);
    }
    if kind == 2 {
        let _nu = rd_u32(blob, p);
        p += 4;
        let dl = rd_u32(blob, p);
        p += 4;
        let distinct: Vec<Vec<u8>> = split_bytes(&zlibw::inflate(&blob[p..p + dl]), &[delim])
            .iter()
            .map(|c| c.to_vec())
            .collect();
        p += dl;
        let sel = blob[p];
        p += 1;
        let il = rd_u32(blob, p);
        p += 4;
        let mut idx = ctxcoder::decode(&blob[p..p + il], n);
        undo_cumsum(&mut idx, sel);
        let cells = idx.iter().map(|&i| distinct[i as usize].clone()).collect();
        return (cells, p + il);
    }
    // kind == 1: numeric
    let ndec = blob[p] as usize;
    let sel = blob[p + 1];
    p += 2;
    let ln = rd_u32(blob, p);
    p += 4;
    let mut vals = ctxcoder::decode(&blob[p..p + ln], n);
    undo_cumsum(&mut vals, sel);
    let cells = vals.iter().map(|&v| fmt(v, ndec)).collect();
    (cells, p + ln)
}

fn undo_cumsum(v: &mut [i64], sel: u8) {
    for _ in 0..sel {
        let mut acc = 0i64;
        for x in v.iter_mut() {
            acc += *x;
            *x = acc;
        }
    }
}

pub fn decode(blob: &[u8]) -> Vec<u8> {
    assert_eq!(&blob[..4], CMAGIC, "not a CSV1 stream");
    match blob[4] {
        M_STORE => return blob[5..].to_vec(),
        M_DEFLATE => return zlibw::inflate(&blob[5..]),
        _ => {}
    }
    let mut p = 5;
    let delim = blob[p];
    let lt: &[u8] = if blob[p + 1] == 0 { b"\n" } else { b"\r\n" };
    let has_tl = blob[p + 2] == 1;
    p += 3;
    let n = rd_u32(blob, p);
    p += 4;
    let k = rd_u16(blob, p);
    p += 2;
    let hl = rd_u32(blob, p);
    p += 4;
    let header = blob[p..p + hl].to_vec();
    p += hl;
    let mut cols: Vec<Vec<Vec<u8>>> = Vec::with_capacity(k);
    for _ in 0..k {
        let (col, np) = decode_col(blob, p, n, delim);
        cols.push(col);
        p = np;
    }
    // reassemble: header then each row (fields joined by delim), all joined by lt
    let mut lines: Vec<Vec<u8>> = Vec::with_capacity(n + 1);
    lines.push(header);
    for r in 0..n {
        let fields: Vec<&[u8]> = (0..k).map(|c| cols[c][r].as_slice()).collect();
        lines.push(join_bytes(&fields, &[delim]));
    }
    let refs: Vec<&[u8]> = lines.iter().map(|l| l.as_slice()).collect();
    let mut out = join_bytes(&refs, lt);
    if has_tl {
        out.extend_from_slice(lt);
    }
    out
}

#[no_mangle]
pub unsafe extern "C" fn csv_encode(data: *const u8, len: i64, out: *mut u8, cap: i64) -> i64 {
    let data = std::slice::from_raw_parts(data, len as usize);
    let blob = encode(data);
    if blob.len() as i64 > cap {
        return -1;
    }
    std::ptr::copy_nonoverlapping(blob.as_ptr(), out, blob.len());
    blob.len() as i64
}

#[no_mangle]
pub unsafe extern "C" fn csv_decode(input: *const u8, len: i64, out: *mut u8, cap: i64) -> i64 {
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
        let mut s = String::from("Date;V;n\n");
        let mut v = 234000i64;
        let mut r: u64 = 5;
        for i in 0..2000 {
            r = r.wrapping_mul(6364136223846793005).wrapping_add(1);
            v += (r >> 60) as i64 - 8;
            s.push_str(&format!("16/12/2006;{}.{:03};{}\n", v / 1000, v % 1000, i));
        }
        let data = s.into_bytes();
        assert_eq!(decode(&encode(&data)), data);
        // CRLF + no trailing newline + ragged (falls back), all lossless
        assert_eq!(decode(&encode(b"a,b\r\n1,2\r\n3,4")), b"a,b\r\n1,2\r\n3,4");
        assert_eq!(decode(&encode(b"a;b;c\n1;2\n3;4;5;6\n")), b"a;b;c\n1;2\n3;4;5;6\n");
        assert_eq!(decode(&encode(b"plain text no grid\n")), b"plain text no grid\n");
    }
}
