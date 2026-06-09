//! Reversible byte-stream transforms — byte-identical to `pertype/transform.py`
//! (and the C `delta_fwd`/`delta_inv`). The building blocks the per-type model selects:
//! stride `delta` (numeric/image decorrelation) and `split` (byte-plane de-interleave).
//! Pure deterministic byte ops, so the output is exactly identical to the Python reference.

pub fn delta_fwd(data: &[u8], stride: usize) -> Vec<u8> {
    let mut out = data.to_vec();
    for i in stride..out.len() {
        out[i] = data[i].wrapping_sub(data[i - stride]);
    }
    out
}

pub fn delta_inv(data: &[u8], stride: usize) -> Vec<u8> {
    let mut out = data.to_vec();
    for i in stride..out.len() {
        out[i] = out[i - stride].wrapping_add(data[i]);
    }
    out
}

/// De-interleave into `n` byte-planes: plane p is bytes at positions p, p+n, p+2n, …
pub fn split_fwd(data: &[u8], n: usize) -> Vec<u8> {
    let mut out = Vec::with_capacity(data.len());
    for p in 0..n {
        let mut i = p;
        while i < data.len() {
            out.push(data[i]);
            i += n;
        }
    }
    out
}

pub fn split_inv(data: &[u8], n: usize) -> Vec<u8> {
    let total = data.len();
    let mut out = vec![0u8; total];
    let mut pos = 0usize;
    for p in 0..n {
        let mut i = p;
        while i < total {
            out[i] = data[pos];
            pos += 1;
            i += n;
        }
    }
    out
}

// --- stride XOR-delta (Gorilla) ---------------------------------------------

pub fn xor_fwd(data: &[u8], stride: usize) -> Vec<u8> {
    let mut out = data.to_vec();
    for i in stride..out.len() {
        out[i] ^= data[i - stride];
    }
    out
}

pub fn xor_inv(data: &[u8], stride: usize) -> Vec<u8> {
    let mut out = data.to_vec();
    for i in stride..out.len() {
        out[i] ^= out[i - stride];
    }
    out
}

// --- FCM/DFCM float64 value prediction (FPC) --------------------------------

const U64M: u64 = 0xFFFF_FFFF_FFFF_FFFF;

#[inline]
fn lead_zero_bytes(r: u64) -> u32 {
    if r == 0 {
        return 8;
    }
    let mut c = 0;
    for p in (0..8).rev() {
        if (r >> (8 * p)) & 0xFF != 0 {
            break;
        }
        c += 1;
    }
    c
}

pub fn fcm_fwd(data: &[u8], bits: u32) -> Vec<u8> {
    let n = data.len();
    let nval = n / 8;
    let mask: u64 = (1u64 << bits) - 1;
    let mut fcm = vec![0u64; 1usize << bits];
    let mut dfcm = vec![0u64; 1usize << bits];
    let (mut fh, mut dh, mut last) = (0u64, 0u64, 0u64);
    let mut sel = vec![0u8; nval];
    let mut res = vec![0u64; nval];
    for i in 0..nval {
        let mut vb = [0u8; 8];
        vb.copy_from_slice(&data[i * 8..i * 8 + 8]);
        let v = u64::from_le_bytes(vb);
        let pf = fcm[fh as usize];
        let pd = last.wrapping_add(dfcm[dh as usize]) & U64M;
        let rf = v ^ pf;
        let rd = v ^ pd;
        if lead_zero_bytes(rd) > lead_zero_bytes(rf) {
            sel[i] = 1;
            res[i] = rd;
        } else {
            res[i] = rf;
        }
        fcm[fh as usize] = v;
        let diff = v.wrapping_sub(last) & U64M;
        dfcm[dh as usize] = diff;
        fh = ((fh << 6) ^ (v >> 48)) & mask;
        dh = ((dh << 2) ^ (diff >> 40)) & mask;
        last = v;
    }
    let mut out = Vec::with_capacity(n);
    out.extend_from_slice(&sel);
    let mut planes = vec![0u8; 8 * nval];
    for i in 0..nval {
        let r = res[i];
        for p in 0..8 {
            planes[p * nval + i] = ((r >> (8 * p)) & 0xFF) as u8;
        }
    }
    out.extend_from_slice(&planes);
    out.extend_from_slice(&data[nval * 8..]);
    out
}

pub fn fcm_inv(data: &[u8], bits: u32) -> Vec<u8> {
    let l = data.len();
    let nval = l / 9; // l = nval + 8*nval + rem, rem < 8 < 9
    let mask: u64 = (1u64 << bits) - 1;
    let mut fcm = vec![0u64; 1usize << bits];
    let mut dfcm = vec![0u64; 1usize << bits];
    let (mut fh, mut dh, mut last) = (0u64, 0u64, 0u64);
    let sel = &data[..nval];
    let planes = &data[nval..nval + 8 * nval];
    let trailing = &data[nval + 8 * nval..];
    let mut out = vec![0u8; 8 * nval];
    for i in 0..nval {
        let mut r = 0u64;
        for p in 0..8 {
            r |= (planes[p * nval + i] as u64) << (8 * p);
        }
        let pf = fcm[fh as usize];
        let pd = last.wrapping_add(dfcm[dh as usize]) & U64M;
        let v = (r ^ (if sel[i] != 0 { pd } else { pf })) & U64M;
        out[i * 8..i * 8 + 8].copy_from_slice(&v.to_le_bytes());
        fcm[fh as usize] = v;
        let diff = v.wrapping_sub(last) & U64M;
        dfcm[dh as usize] = diff;
        fh = ((fh << 6) ^ (v >> 48)) & mask;
        dh = ((dh << 2) ^ (diff >> 40)) & mask;
        last = v;
    }
    out.extend_from_slice(trailing);
    out
}

/// Apply a transform spec (each `(code, arg)`: 0=delta 1=split 2=xor 3=fcm) in order.
pub fn apply(data: &[u8], spec: &[(u8, u8)]) -> Vec<u8> {
    let mut d = data.to_vec();
    for &(op, arg) in spec {
        d = match op {
            0 => delta_fwd(&d, arg as usize),
            1 => split_fwd(&d, arg as usize),
            2 => xor_fwd(&d, arg as usize),
            3 => fcm_fwd(&d, arg as u32),
            _ => d,
        };
    }
    d
}

/// Invert a transform spec (ops applied in reverse).
pub fn invert(data: &[u8], spec: &[(u8, u8)]) -> Vec<u8> {
    let mut d = data.to_vec();
    for &(op, arg) in spec.iter().rev() {
        d = match op {
            0 => delta_inv(&d, arg as usize),
            1 => split_inv(&d, arg as usize),
            2 => xor_inv(&d, arg as usize),
            3 => fcm_inv(&d, arg as u32),
            _ => d,
        };
    }
    d
}

/// Candidate transform pipelines tried by training (op codes: 0=delta 1=split 2=xor 3=fcm).
/// Mirrors `transform.TRANSFORM_SPECS`.
pub fn transform_specs() -> Vec<Vec<(u8, u8)>> {
    vec![
        vec![],
        vec![(0, 1)],
        vec![(0, 2)],
        vec![(0, 4)],
        vec![(1, 2)],
        vec![(1, 2), (0, 1)],
        vec![(1, 2), (0, 2)],
        vec![(0, 4), (1, 2)],
        vec![(2, 8)],
        vec![(2, 8), (1, 8)],
        vec![(1, 8)],
        vec![(2, 4), (1, 4)],
        vec![(3, 16)],
    ]
}

/// Pick the transform that most shrinks the data under a zlib proxy (level 6) — byte-for-byte
/// the same *procedure* as `transform.select`, but the ranking uses flate2's deflate, which
/// can disagree with CPython's on near-ties (so the chosen spec may differ on borderline
/// numeric data; the resulting model is still valid and cross-loadable).
pub fn select(samples: &[&[u8]]) -> Vec<(u8, u8)> {
    const CAP: usize = 1 << 21;
    const SLOW_CAP: usize = 1 << 18;
    let mut blob: Vec<u8> = samples.concat();
    if blob.len() > CAP {
        blob.truncate(CAP);
    }
    if blob.is_empty() {
        return vec![];
    }
    let zsize = |spec: &[(u8, u8)], b: &[u8]| crate::zlibw::deflate_level(&apply(b, spec), 6).len();
    let specs = transform_specs();
    let is_slow = |s: &[(u8, u8)]| s.iter().any(|&(op, _)| op == 3);

    let mut best: Vec<(u8, u8)> = vec![];
    let mut best_size: Option<usize> = None;
    for spec in specs.iter().filter(|s| !is_slow(s)) {
        let size = zsize(spec, &blob);
        if best_size.map_or(true, |b| size < b) {
            best = spec.clone();
            best_size = Some(size);
        }
    }
    let slow: Vec<&Vec<(u8, u8)>> = specs.iter().filter(|s| is_slow(s)).collect();
    if !slow.is_empty() {
        let sample = &blob[..SLOW_CAP.min(blob.len())];
        let mut incumbent = zsize(&best, sample);
        for spec in slow {
            let size = zsize(spec, sample);
            if size < incumbent {
                best = spec.clone();
                incumbent = size;
            }
        }
    }
    best
}

/// Serialize a transform spec — `[len, (code, arg)…]`, matching `transform.serialize`.
pub fn serialize(spec: &[(u8, u8)]) -> Vec<u8> {
    let mut out = vec![spec.len() as u8];
    for &(op, arg) in spec {
        out.push(op);
        out.push(arg);
    }
    out
}

unsafe fn run(f: fn(&[u8], usize) -> Vec<u8>, data: *const u8, len: i64, arg: i64, out: *mut u8) {
    let v = f(std::slice::from_raw_parts(data, len as usize), arg as usize);
    std::ptr::copy_nonoverlapping(v.as_ptr(), out, v.len());
}

#[no_mangle]
pub unsafe extern "C" fn transform_delta_fwd(data: *const u8, len: i64, stride: i64, out: *mut u8) {
    run(delta_fwd, data, len, stride, out)
}
#[no_mangle]
pub unsafe extern "C" fn transform_delta_inv(data: *const u8, len: i64, stride: i64, out: *mut u8) {
    run(delta_inv, data, len, stride, out)
}
#[no_mangle]
pub unsafe extern "C" fn transform_split_fwd(data: *const u8, len: i64, n: i64, out: *mut u8) {
    run(split_fwd, data, len, n, out)
}
#[no_mangle]
pub unsafe extern "C" fn transform_split_inv(data: *const u8, len: i64, n: i64, out: *mut u8) {
    run(split_inv, data, len, n, out)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn roundtrip() {
        let data: Vec<u8> = (0..1000u32).map(|i| (i.wrapping_mul(37) >> 3) as u8).collect();
        for s in [1usize, 2, 4] {
            assert_eq!(delta_inv(&delta_fwd(&data, s), s), data);
        }
        for n in [2usize, 3, 8] {
            assert_eq!(split_inv(&split_fwd(&data, n), n), data);
        }
    }
}
