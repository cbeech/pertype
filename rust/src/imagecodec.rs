//! Lossless image codec — byte-identical to `compressor/imagecodec.py`. Per-plane choice of
//! MED / CALIC / RLE (cheapest wins), gray / Bayer / RGB modes, and inter-slice-delta
//! volumes. Plane coding is all arithmetic (ctxcoder/CALIC) and the checksum is standard
//! crc32, so the `RIMG`/`RVOL` containers are exact twins of the Python output.

use rayon::prelude::*;

use crate::calic;
use crate::ctxcoder;
use crate::predictors;

const MAGIC: &[u8] = b"RIMG";
const VMAGIC: &[u8] = b"RVOL";
const VERSION: u8 = 4;
pub const GRAY: u8 = 0;
pub const BAYER: u8 = 1;
pub const RGB: u8 = 2;
const MED: u8 = 0;
const CALIC: u8 = 3;
const RLE: u8 = 4;

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
fn crc32(data: &[u8]) -> u32 {
    let mut c = flate2::Crc::new();
    c.update(data);
    c.sum()
}
fn rdv(d: &[u8], i: usize, isz: usize, signed: bool) -> i64 {
    if isz == 1 {
        d[i] as i64
    } else if signed {
        i16::from_le_bytes([d[2 * i], d[2 * i + 1]]) as i64 // int16 sign-extends (e.g. DEM)
    } else {
        u16::from_le_bytes([d[2 * i], d[2 * i + 1]]) as i64 // uint16 zero-extends (e.g. DICOM)
    }
}
fn wrv(o: &mut [u8], i: usize, v: i64, isz: usize) {
    if isz == 1 {
        o[i] = v as u8;
    } else {
        let b = (v as u16).to_le_bytes();
        o[2 * i] = b[0];
        o[2 * i + 1] = b[1];
    }
}

// --- per-plane coding --------------------------------------------------------

struct Plane {
    v: Vec<i64>,
    h: usize,
    w: usize,
}

fn scales(p: &[i64]) -> Vec<i64> {
    if p.is_empty() {
        return vec![1];
    }
    let (mut lo, mut hi) = (p[0], p[0]);
    for &x in p {
        lo = lo.min(x);
        hi = hi.max(x);
    }
    let k = ((hi - lo) / 256).max(1);
    if k == 1 {
        vec![1]
    } else {
        vec![1, k]
    }
}

fn rle_encode(flat: &[i64]) -> Vec<u8> {
    if flat.is_empty() {
        return u32be(0).to_vec();
    }
    let mut starts = vec![0usize];
    for i in 1..flat.len() {
        if flat[i] != flat[i - 1] {
            starts.push(i);
        }
    }
    let vals: Vec<i64> = starts.iter().map(|&s| flat[s]).collect();
    let mut lens: Vec<i64> = Vec::with_capacity(starts.len());
    for i in 0..starts.len() {
        let end = if i + 1 < starts.len() { starts[i + 1] } else { flat.len() };
        lens.push((end - starts[i]) as i64);
    }
    let vb = ctxcoder::encode(&vals);
    let lb = ctxcoder::encode(&lens);
    let mut out = u32be(starts.len()).to_vec();
    out.extend_from_slice(&u32be(vb.len()));
    out.extend_from_slice(&vb);
    out.extend_from_slice(&lb);
    out
}

fn rle_decode(blob: &[u8], h: usize, w: usize) -> Vec<i64> {
    let nruns = rd_u32(blob, 0);
    if nruns == 0 {
        return vec![0i64; h * w];
    }
    let vlen = rd_u32(blob, 4);
    let vals = ctxcoder::decode(&blob[8..8 + vlen], nruns);
    let lens = ctxcoder::decode(&blob[8 + vlen..], nruns);
    let mut out = Vec::with_capacity(h * w);
    for (v, l) in vals.iter().zip(lens.iter()) {
        for _ in 0..*l {
            out.push(*v);
        }
    }
    out
}

fn code_plane(p: &Plane) -> (u8, u16, Vec<u8>) {
    let mut best = (MED, 1u16, ctxcoder::encode(&predictors::med_residual(&p.v, p.h, p.w)));
    for sc in scales(&p.v) {
        let blob = calic::encode(&p.v, p.h, p.w, sc);
        if blob.len() < best.2.len() {
            best = (CALIC, sc as u16, blob);
        }
    }
    let rb = rle_encode(&p.v);
    if rb.len() < best.2.len() {
        best = (RLE, 1, rb);
    }
    best
}

fn decode_plane(code: u8, scale: u16, blob: &[u8], h: usize, w: usize) -> Vec<i64> {
    match code {
        CALIC => calic::decode(blob, h, w, scale as i64),
        RLE => rle_decode(blob, h, w),
        MED => predictors::med_reconstruct(&ctxcoder::decode(blob, h * w), h, w),
        other => panic!("plane coder {other} not supported by the Rust decoder"),
    }
}

fn pack_plane(code: u8, scale: u16, blob: &[u8], out: &mut Vec<u8>) {
    out.push(code);
    out.extend_from_slice(&u16be(scale as usize));
    out.extend_from_slice(&u32be(blob.len()));
    out.extend_from_slice(blob);
}

// --- gray / Bayer / RGB split + merge ----------------------------------------

fn bayer_dims(h: usize, w: usize, oy: usize, ox: usize) -> (usize, usize) {
    ((h - oy + 1) / 2, (w - ox + 1) / 2)
}
const BAYER_OFF: [(usize, usize); 4] = [(0, 0), (0, 1), (1, 0), (1, 1)];

fn split(data: &[u8], mode: u8, h: usize, w: usize, isz: usize, sg: bool) -> Vec<Plane> {
    match mode {
        BAYER => BAYER_OFF
            .iter()
            .map(|&(oy, ox)| {
                let (ph, pw) = bayer_dims(h, w, oy, ox);
                let mut v = vec![0i64; ph * pw];
                for py in 0..ph {
                    for px in 0..pw {
                        v[py * pw + px] = rdv(data, (oy + 2 * py) * w + (ox + 2 * px), isz, sg);
                    }
                }
                Plane { v, h: ph, w: pw }
            })
            .collect(),
        RGB => {
            let n = h * w;
            let mut g = vec![0i64; n];
            let mut rmg = vec![0i64; n];
            let mut bmg = vec![0i64; n];
            for i in 0..n {
                let (r, gg, b) =
                    (rdv(data, i * 3, isz, sg), rdv(data, i * 3 + 1, isz, sg), rdv(data, i * 3 + 2, isz, sg));
                g[i] = gg;
                rmg[i] = r - gg;
                bmg[i] = b - gg;
            }
            vec![Plane { v: g, h, w }, Plane { v: rmg, h, w }, Plane { v: bmg, h, w }]
        }
        _ => {
            let v: Vec<i64> = (0..h * w).map(|i| rdv(data, i, isz, sg)).collect();
            vec![Plane { v, h, w }]
        }
    }
}

fn plane_shapes(mode: u8, h: usize, w: usize) -> Vec<(usize, usize)> {
    match mode {
        BAYER => BAYER_OFF.iter().map(|&(oy, ox)| bayer_dims(h, w, oy, ox)).collect(),
        RGB => vec![(h, w); 3],
        _ => vec![(h, w)],
    }
}

fn merge(planes: &[Vec<i64>], mode: u8, h: usize, w: usize, isz: usize) -> Vec<u8> {
    let elems = if mode == RGB { h * w * 3 } else { h * w };
    let mut out = vec![0u8; elems * isz];
    match mode {
        BAYER => {
            for (k, &(oy, ox)) in BAYER_OFF.iter().enumerate() {
                let (ph, pw) = bayer_dims(h, w, oy, ox);
                for py in 0..ph {
                    for px in 0..pw {
                        wrv(&mut out, (oy + 2 * py) * w + (ox + 2 * px), planes[k][py * pw + px], isz);
                    }
                }
            }
        }
        RGB => {
            let (g, rmg, bmg) = (&planes[0], &planes[1], &planes[2]);
            for i in 0..h * w {
                wrv(&mut out, i * 3, rmg[i] + g[i], isz);
                wrv(&mut out, i * 3 + 1, g[i], isz);
                wrv(&mut out, i * 3 + 2, bmg[i] + g[i], isz);
            }
        }
        _ => {
            for i in 0..h * w {
                wrv(&mut out, i, planes[0][i], isz);
            }
        }
    }
    out
}

// --- public API --------------------------------------------------------------

pub fn encode(data: &[u8], h: usize, w: usize, isz: usize, mode: u8, sg: bool) -> Vec<u8> {
    let planes = split(data, mode, h, w, isz, sg);
    let parts: Vec<(u8, u16, Vec<u8>)> = planes.par_iter().map(code_plane).collect();
    let mut out = Vec::new();
    out.extend_from_slice(MAGIC);
    out.extend_from_slice(&[VERSION, mode, isz as u8]);
    out.extend_from_slice(&u32be(h));
    out.extend_from_slice(&u32be(w));
    out.extend_from_slice(&u32be(crc32(data) as usize));
    out.push(parts.len() as u8);
    for (c, s, b) in &parts {
        pack_plane(*c, *s, b, &mut out);
    }
    out
}

pub fn decode(blob: &[u8]) -> Vec<u8> {
    assert!(&blob[..4] == MAGIC && blob[4] == VERSION, "not a RIMG v4 container");
    let mode = blob[5];
    let isz = blob[6] as usize;
    let h = rd_u32(blob, 7);
    let w = rd_u32(blob, 11);
    let crc = rd_u32(blob, 15) as u32;
    let n_planes = blob[19] as usize;
    let shapes = plane_shapes(mode, h, w);
    assert_eq!(shapes.len(), n_planes, "plane count mismatch");
    let mut p = 20;
    let mut planes = Vec::with_capacity(n_planes);
    for (ph, pw) in shapes {
        let code = blob[p];
        let scale = rd_u16(blob, p + 1) as u16;
        let n = rd_u32(blob, p + 3);
        let chunk = &blob[p + 7..p + 7 + n];
        p += 7 + n;
        planes.push(decode_plane(code, scale, chunk, ph, pw));
    }
    let out = merge(&planes, mode, h, w, isz);
    assert_eq!(crc32(&out), crc, "checksum mismatch");
    out
}

pub fn encode_volume(data: &[u8], n: usize, h: usize, w: usize, isz: usize, sg: bool) -> Vec<u8> {
    let hw = h * w;
    let slices: Vec<Vec<i64>> = (0..n)
        .map(|s| (0..hw).map(|i| rdv(data, s * hw + i, isz, sg)).collect())
        .collect();
    let planes: Vec<Plane> = (0..n)
        .map(|s| {
            let v = if s == 0 {
                slices[s].clone()
            } else {
                (0..hw).map(|i| slices[s][i] - slices[s - 1][i]).collect()
            };
            Plane { v, h, w }
        })
        .collect();
    let parts: Vec<(u8, u16, Vec<u8>)> = planes.par_iter().map(code_plane).collect();
    let mut out = Vec::new();
    out.extend_from_slice(VMAGIC);
    out.extend_from_slice(&[VERSION, isz as u8]);
    out.extend_from_slice(&u16be(n));
    out.extend_from_slice(&u32be(h));
    out.extend_from_slice(&u32be(w));
    out.extend_from_slice(&u32be(crc32(data) as usize));
    for (c, s, b) in &parts {
        pack_plane(*c, *s, b, &mut out);
    }
    out
}

pub fn decode_volume(blob: &[u8]) -> Vec<u8> {
    assert!(&blob[..4] == VMAGIC && blob[4] == VERSION, "not a RVOL v4 container");
    let isz = blob[5] as usize;
    let n = rd_u16(blob, 6);
    let h = rd_u32(blob, 8);
    let w = rd_u32(blob, 12);
    let crc = rd_u32(blob, 16) as u32;
    let hw = h * w;
    let mut p = 20;
    let mut out = vec![0u8; n * hw * isz];
    let mut prev: Vec<i64> = Vec::new();
    for s in 0..n {
        let code = blob[p];
        let scale = rd_u16(blob, p + 1) as u16;
        let ln = rd_u32(blob, p + 3);
        let chunk = &blob[p + 7..p + 7 + ln];
        p += 7 + ln;
        let plane = decode_plane(code, scale, chunk, h, w);
        let cur: Vec<i64> = if s == 0 {
            plane
        } else {
            (0..hw).map(|i| plane[i] + prev[i]).collect()
        };
        for i in 0..hw {
            wrv(&mut out, s * hw + i, cur[i], isz);
        }
        prev = cur;
    }
    assert_eq!(crc32(&out), crc, "checksum mismatch");
    out
}

#[no_mangle]
pub unsafe extern "C" fn image_encode(
    data: *const u8, len: i64, h: i64, w: i64, isz: i64, mode: i64, sg: i64, out: *mut u8, cap: i64,
) -> i64 {
    let data = std::slice::from_raw_parts(data, len as usize);
    let blob = encode(data, h as usize, w as usize, isz as usize, mode as u8, sg != 0);
    if blob.len() as i64 > cap {
        return -1;
    }
    std::ptr::copy_nonoverlapping(blob.as_ptr(), out, blob.len());
    blob.len() as i64
}

#[no_mangle]
pub unsafe extern "C" fn image_decode(input: *const u8, len: i64, out: *mut u8, cap: i64) -> i64 {
    let v = decode(std::slice::from_raw_parts(input, len as usize));
    if v.len() as i64 > cap {
        return -1;
    }
    std::ptr::copy_nonoverlapping(v.as_ptr(), out, v.len());
    v.len() as i64
}

#[no_mangle]
pub unsafe extern "C" fn volume_encode(
    data: *const u8, len: i64, n: i64, h: i64, w: i64, isz: i64, sg: i64, out: *mut u8, cap: i64,
) -> i64 {
    let data = std::slice::from_raw_parts(data, len as usize);
    let blob = encode_volume(data, n as usize, h as usize, w as usize, isz as usize, sg != 0);
    if blob.len() as i64 > cap {
        return -1;
    }
    std::ptr::copy_nonoverlapping(blob.as_ptr(), out, blob.len());
    blob.len() as i64
}

#[no_mangle]
pub unsafe extern "C" fn volume_decode(input: *const u8, len: i64, out: *mut u8, cap: i64) -> i64 {
    let v = decode_volume(std::slice::from_raw_parts(input, len as usize));
    if v.len() as i64 > cap {
        return -1;
    }
    std::ptr::copy_nonoverlapping(v.as_ptr(), out, v.len());
    v.len() as i64
}
