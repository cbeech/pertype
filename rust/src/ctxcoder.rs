//! Context-adaptive arithmetic residual coder — byte-identical to `pertype/ctxcoder.py`
//! and the C twin. Order-2 magnitude-bucket model (prev, prev-prev) + top-mantissa-bit
//! model per (context, k); the rest raw.

use crate::arith::*;

const CLAMP: i32 = 16;
const NCTX: usize = ((CLAMP + 1) * (CLAMP + 1)) as usize; // 289

#[inline]
fn ctx_index(pk: i32, pk2: i32) -> usize {
    let a = pk.min(CLAMP);
    let b = pk2.min(CLAMP);
    (a * (CLAMP + 1) + b) as usize
}

pub fn encode(res: &[i64]) -> Vec<u8> {
    let mut freq = vec![[1i32; NB]; NCTX];
    let mut tot = vec![NB as i64; NCTX];
    let mut mf = vec![[1i32; 2]; NCTX * NB];
    let mut mt = vec![2i64; NCTX * NB];
    let mut e = Enc::new();
    let (mut pk, mut pk2) = (0i32, 0i32);
    for &r in res {
        let u = zigzag(r);
        let k = bit_length(u);
        let ctx = ctx_index(pk, pk2);
        let f = &mut freq[ctx];
        let mut cum = 0u64;
        for s in 0..k {
            cum += f[s] as u64;
        }
        e.encode(cum, f[k] as u64, tot[ctx] as u64);
        if k >= 2 {
            let mant = u & ((1u64 << (k - 1)) - 1);
            let b1 = ((mant >> (k - 2)) & 1) as usize;
            let mi = ctx * NB + k;
            let g = &mut mf[mi];
            e.encode(if b1 == 0 { 0 } else { g[0] as u64 }, g[b1] as u64, mt[mi] as u64);
            g[b1] += MINCR;
            mt[mi] += MINCR as i64;
            if mt[mi] >= MRESCALE {
                g[0] = (g[0] + 1) >> 1;
                g[1] = (g[1] + 1) >> 1;
                mt[mi] = (g[0] + g[1]) as i64;
            }
            for sh in (0..k - 2).rev() {
                e.encode((mant >> sh) & 1, 1, 2);
            }
        }
        f[k] += INCR as i32;
        tot[ctx] += INCR;
        if tot[ctx] >= RESCALE {
            let mut t = 0i64;
            for s in 0..NB {
                f[s] = (f[s] + 1) >> 1;
                t += f[s] as i64;
            }
            tot[ctx] = t;
        }
        pk2 = pk;
        pk = k as i32;
    }
    e.finish()
}

pub fn decode(inp: &[u8], n: usize) -> Vec<i64> {
    let mut freq = vec![[1i32; NB]; NCTX];
    let mut tot = vec![NB as i64; NCTX];
    let mut mf = vec![[1i32; 2]; NCTX * NB];
    let mut mt = vec![2i64; NCTX * NB];
    let mut d = Dec::new(inp);
    let (mut pk, mut pk2) = (0i32, 0i32);
    let mut out = Vec::with_capacity(n);
    for _ in 0..n {
        let ctx = ctx_index(pk, pk2);
        let f = &mut freq[ctx];
        let total = tot[ctx] as u64;
        let target = d.target(total);
        let mut cum = 0u64;
        let mut k = 0usize;
        while cum + f[k] as u64 <= target {
            cum += f[k] as u64;
            k += 1;
        }
        d.update(cum, f[k] as u64, total);
        let u: u64 = if k == 0 {
            0
        } else if k == 1 {
            1
        } else {
            let mi = ctx * NB + k;
            let g = &mut mf[mi];
            let b1 = if d.target(mt[mi] as u64) >= g[0] as u64 { 1usize } else { 0 };
            d.update(if b1 == 0 { 0 } else { g[0] as u64 }, g[b1] as u64, mt[mi] as u64);
            g[b1] += MINCR;
            mt[mi] += MINCR as i64;
            if mt[mi] >= MRESCALE {
                g[0] = (g[0] + 1) >> 1;
                g[1] = (g[1] + 1) >> 1;
                mt[mi] = (g[0] + g[1]) as i64;
            }
            let mut low = 0u64;
            for _ in 0..k - 2 {
                let bit = if d.target(2) >= 1 { 1u64 } else { 0 };
                d.update(bit, 1, 2);
                low = (low << 1) | bit;
            }
            (1u64 << (k - 1)) | ((b1 as u64) << (k - 2)) | low
        };
        out.push(unzigzag(u));
        f[k] += INCR as i32;
        tot[ctx] += INCR;
        if tot[ctx] >= RESCALE {
            let mut t = 0i64;
            for s in 0..NB {
                f[s] = (f[s] + 1) >> 1;
                t += f[s] as i64;
            }
            tot[ctx] = t;
        }
        pk2 = pk;
        pk = k as i32;
    }
    out
}

/// Smallest of raw / delta / second-difference of an int column under this coder; ties
/// keep the earlier (matches Python's `min`). Shared by the columnar / float / CSV codecs.
pub fn code_idx(col: &[i64]) -> (u8, Vec<u8>) {
    let mut d = col.to_vec();
    for i in 1..d.len() {
        d[i] = col[i] - col[i - 1];
    }
    let mut dd = d.clone();
    for i in 1..dd.len() {
        dd[i] = d[i] - d[i - 1];
    }
    let mut best = (0u8, encode(col));
    for (sel, blob) in [(1u8, encode(&d)), (2u8, encode(&dd))] {
        if blob.len() < best.1.len() {
            best = (sel, blob);
        }
    }
    best
}

#[no_mangle]
pub unsafe extern "C" fn ctx_encode(res: *const i64, n: i64, out: *mut u8, cap: i64) -> i64 {
    let res = std::slice::from_raw_parts(res, n as usize);
    let bytes = encode(res);
    if bytes.len() as i64 > cap {
        return -1;
    }
    std::ptr::copy_nonoverlapping(bytes.as_ptr(), out, bytes.len());
    bytes.len() as i64
}

#[no_mangle]
pub unsafe extern "C" fn ctx_decode(input: *const u8, len: i64, n: i64, out: *mut i64) {
    let inp = std::slice::from_raw_parts(input, len as usize);
    let vals = decode(inp, n as usize);
    std::ptr::copy_nonoverlapping(vals.as_ptr(), out, vals.len());
}

#[cfg(test)]
mod tests {
    use super::*;
    fn rt(v: &[i64]) {
        assert_eq!(decode(&encode(v), v.len()), v);
    }
    #[test]
    fn roundtrip() {
        rt(&[]);
        rt(&[0; 100]);
        rt(&[0, 1, -1, 2, -2, 1_000_000, -1_000_000]);
        let mut x = 0i64;
        let mut v = Vec::new();
        let mut s: u64 = 0x1234_5678;
        for _ in 0..20000 {
            s = s.wrapping_mul(6364136223846793005).wrapping_add(1);
            x += (s >> 60) as i64 - 8;
            v.push(x);
        }
        rt(&v);
    }
}
