//! Full CALIC image codec — byte-identical to `_calic_codec_py` and the C `calic_codec`.
//! GAP prediction + 704-context running-mean bias correction + energy-conditional
//! magnitude-bucket coding with the top mantissa bit modelled per (energy-bin, k).

use crate::arith::*;

const NEBIN: usize = 12;
const NCTX: usize = 704;

#[inline]
fn calic_round(b: i64, c: i64) -> i64 {
    if c <= 0 {
        0
    } else if b >= 0 {
        (b + c / 2) / c
    } else {
        -(((-b) + c / 2) / c)
    }
}

struct Model {
    bb: Vec<i64>,
    cc: Vec<i64>,
    freq: Vec<[i32; NB]>,
    tot: Vec<i64>,
    mf: Vec<[i32; 2]>,
    mt: Vec<i64>,
    tbias: Vec<i64>,
    tent: Vec<i64>,
    t1: i64,
    t2: i64,
    t3: i64,
}

impl Model {
    fn new(scale: i64) -> Self {
        Model {
            bb: vec![0i64; NCTX],
            cc: vec![0i64; NCTX],
            freq: vec![[1i32; NB]; NEBIN],
            tot: vec![NB as i64; NEBIN],
            mf: vec![[1i32; 2]; NEBIN * NB],
            mt: vec![2i64; NEBIN * NB],
            tbias: [1, 3, 6, 11, 18, 30, 50, 90, 160, 300].iter().map(|t| t * scale).collect(),
            tent: [1, 3, 6, 11, 18, 30, 50, 90, 160, 300, 600].iter().map(|t| t * scale).collect(),
            t1: 80 * scale,
            t2: 32 * scale,
            t3: 8 * scale,
        }
    }
}

/// Per-pixel prediction context shared by encode and decode. Returns (pred, kb, ebin).
#[inline]
fn context(m: &Model, img: &[i64], w: usize, y: usize, x: usize, e_left: i64) -> (i64, usize, usize) {
    let i = y * w + x;
    let a = if x > 0 { img[i - 1] } else { 0 };
    let b = if y > 0 { img[i - w] } else { 0 };
    let nw = if x > 0 && y > 0 { img[i - w - 1] } else { 0 };
    let ne = if y > 0 && x < w - 1 { img[i - w + 1] } else { 0 };
    let ww = if x > 1 { img[i - 2] } else { 0 };
    let nn = if y > 1 { img[i - 2 * w] } else { 0 };
    let dh = (a - ww).abs() + (b - nw).abs() + (b - ne).abs();
    let dv = (a - nw).abs() + (b - nn).abs() + (ne - nn).abs();
    let pred = if y == 0 && x == 0 {
        128
    } else if y == 0 {
        a
    } else if x == 0 {
        b
    } else {
        let base = ((a + b) >> 1) + ((ne - nw) >> 2);
        let d = dv - dh;
        if d > m.t1 { a }
        else if d < -m.t1 { b }
        else if d > m.t2 { (base + a) >> 1 }
        else if d < -m.t2 { (base + b) >> 1 }
        else if d > m.t3 { (3 * base + a) >> 2 }
        else if d < -m.t3 { (3 * base + b) >> 2 }
        else { base }
    };
    let mut db = 0usize;
    let ebias = dh + dv + 2 * e_left.abs();
    while db < 10 && ebias >= m.tbias[db] {
        db += 1;
    }
    let tex = (a >= pred) as i64
        | (((b >= pred) as i64) << 1)
        | (((nw >= pred) as i64) << 2)
        | (((ne >= pred) as i64) << 3)
        | (((ww >= pred) as i64) << 4)
        | (((nn >= pred) as i64) << 5);
    let kb = (db as i64 * 64 + tex) as usize;
    let mut ebin = 0usize;
    let en = dh + dv;
    while ebin < 11 && en >= m.tent[ebin] {
        ebin += 1;
    }
    (pred, kb, ebin)
}

#[inline]
fn bump(m: &mut Model, ebin: usize, k: usize) {
    m.freq[ebin][k] += INCR as i32;
    m.tot[ebin] += INCR;
    if m.tot[ebin] >= RESCALE {
        let mut t = 0i64;
        let f = &mut m.freq[ebin];
        for s in 0..NB {
            f[s] = (f[s] + 1) >> 1;
            t += f[s] as i64;
        }
        m.tot[ebin] = t;
    }
}

pub fn encode(img: &[i64], h: usize, w: usize, scale: i64) -> Vec<u8> {
    let mut m = Model::new(scale);
    let mut e = Enc::new();
    for y in 0..h {
        let mut e_left = 0i64;
        for x in 0..w {
            let (pred, kb, ebin) = context(&m, img, w, y, x, e_left);
            let corr = calic_round(m.bb[kb], m.cc[kb]);
            let ev = img[y * w + x] - pred;
            let u = zigzag(ev - corr);
            let k = bit_length(u);
            let f = &m.freq[ebin];
            let mut cum = 0u64;
            for s in 0..k {
                cum += f[s] as u64;
            }
            e.encode(cum, f[k] as u64, m.tot[ebin] as u64);
            if k >= 2 {
                let mant = u & ((1u64 << (k - 1)) - 1);
                let b1 = ((mant >> (k - 2)) & 1) as usize;
                let mi = ebin * NB + k;
                let g = &mut m.mf[mi];
                e.encode(if b1 == 0 { 0 } else { g[0] as u64 }, g[b1] as u64, m.mt[mi] as u64);
                g[b1] += MINCR;
                m.mt[mi] += MINCR as i64;
                if m.mt[mi] >= MRESCALE {
                    let g = &mut m.mf[mi];
                    g[0] = (g[0] + 1) >> 1;
                    g[1] = (g[1] + 1) >> 1;
                    m.mt[mi] = (g[0] + g[1]) as i64;
                }
                for sh in (0..k - 2).rev() {
                    e.encode((mant >> sh) & 1, 1, 2);
                }
            }
            bump(&mut m, ebin, k);
            m.bb[kb] += ev;
            m.cc[kb] += 1;
            if m.cc[kb] >= 256 {
                m.bb[kb] >>= 1;
                m.cc[kb] >>= 1;
            }
            e_left = ev;
        }
    }
    e.finish()
}

pub fn decode(inp: &[u8], h: usize, w: usize, scale: i64) -> Vec<i64> {
    let mut m = Model::new(scale);
    let mut d = Dec::new(inp);
    let mut img = vec![0i64; h * w];
    for y in 0..h {
        let mut e_left = 0i64;
        for x in 0..w {
            let (pred, kb, ebin) = context(&m, &img, w, y, x, e_left);
            let corr = calic_round(m.bb[kb], m.cc[kb]);
            let total = m.tot[ebin] as u64;
            let target = d.target(total);
            let f = &m.freq[ebin];
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
                let mi = ebin * NB + k;
                let g = &mut m.mf[mi];
                let b1 = if d.target(m.mt[mi] as u64) >= g[0] as u64 { 1usize } else { 0 };
                d.update(if b1 == 0 { 0 } else { g[0] as u64 }, g[b1] as u64, m.mt[mi] as u64);
                g[b1] += MINCR;
                m.mt[mi] += MINCR as i64;
                if m.mt[mi] >= MRESCALE {
                    let g = &mut m.mf[mi];
                    g[0] = (g[0] + 1) >> 1;
                    g[1] = (g[1] + 1) >> 1;
                    m.mt[mi] = (g[0] + g[1]) as i64;
                }
                let mut low = 0u64;
                for _ in 0..k - 2 {
                    let bit = if d.target(2) >= 1 { 1u64 } else { 0 };
                    d.update(bit, 1, 2);
                    low = (low << 1) | bit;
                }
                (1u64 << (k - 1)) | ((b1 as u64) << (k - 2)) | low
            };
            let ev = unzigzag(u) + corr;
            img[y * w + x] = ev + pred;
            bump(&mut m, ebin, k);
            m.bb[kb] += ev;
            m.cc[kb] += 1;
            if m.cc[kb] >= 256 {
                m.bb[kb] >>= 1;
                m.cc[kb] >>= 1;
            }
            e_left = ev;
        }
    }
    img
}

#[no_mangle]
pub unsafe extern "C" fn calic_codec_encode(
    img: *const i64, h: i64, w: i64, scale: i64, out: *mut u8, cap: i64,
) -> i64 {
    let img = std::slice::from_raw_parts(img, (h * w) as usize);
    let bytes = encode(img, h as usize, w as usize, scale);
    if bytes.len() as i64 > cap {
        return -1;
    }
    std::ptr::copy_nonoverlapping(bytes.as_ptr(), out, bytes.len());
    bytes.len() as i64
}

#[no_mangle]
pub unsafe extern "C" fn calic_codec_decode(
    input: *const u8, len: i64, img: *mut i64, h: i64, w: i64, scale: i64,
) {
    let inp = std::slice::from_raw_parts(input, len as usize);
    let v = decode(inp, h as usize, w as usize, scale);
    std::ptr::copy_nonoverlapping(v.as_ptr(), img, v.len());
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn roundtrip() {
        let (h, w) = (40usize, 48usize);
        let mut img = vec![0i64; h * w];
        let mut s: u64 = 99;
        for y in 0..h {
            let mut v = 100i64;
            for x in 0..w {
                s = s.wrapping_mul(6364136223846793005).wrapping_add(1);
                v = (v + (s >> 61) as i64 - 3).rem_euclid(256);
                img[y * w + x] = v;
            }
        }
        for scale in [1i64, 4] {
            assert_eq!(decode(&encode(&img, h, w, scale), h, w, scale), img);
        }
    }
}
