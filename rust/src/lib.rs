//! Rust port of the context-adaptive arithmetic residual coder (`compressor/ctxcoder.py`
//! and its C twin in `_native/audio.c`). **Byte-identical** to both: the same
//! Witten–Neal–Cleary 32-bit arithmetic coder driving a per-context magnitude-bucket
//! model with the top mantissa bit modelled per (context, k) and the rest raw, MSB-first
//! with a zero-padded final byte. A file encoded by Python/C decodes here and vice versa.
//!
//! Exposed with the C ABI so it drops in behind the existing ctypes loader (a Rust
//! `cdylib` in place of the C `.so`) — the first piece of an eventual standalone crate.

const NB: usize = 65; // buckets 0..64 cover any int64 zigzag magnitude
const CLAMP: i32 = 16;
const NCTX: usize = ((CLAMP + 1) * (CLAMP + 1)) as usize; // order-2 (prev, prev-prev)
const INCR: i64 = 32;
const RESCALE: i64 = 1 << 14;
const MINCR: i32 = 24;
const MRESCALE: i64 = 1 << 13;
const AC_HALF: u64 = 0x8000_0000;
const AC_QUARTER: u64 = 0x4000_0000;
const AC_3QUARTER: u64 = 0xC000_0000;
const AC_MAX: u64 = 0xFFFF_FFFF;

#[inline]
fn zigzag(r: i64) -> u64 {
    ((r as u64) << 1) ^ ((r >> 63) as u64)
}

#[inline]
fn bit_length(u: u64) -> usize {
    if u == 0 { 0 } else { 64 - u.leading_zeros() as usize }
}

#[inline]
fn ctx_index(pk: i32, pk2: i32) -> usize {
    let a = pk.min(CLAMP);
    let b = pk2.min(CLAMP);
    (a * (CLAMP + 1) + b) as usize
}

// --- MSB-first bit writer + arithmetic encoder -------------------------------

struct Enc {
    out: Vec<u8>,
    cur: u32,
    nbits: u32,
    low: u64,
    high: u64,
    pending: i64,
}

impl Enc {
    fn new() -> Self {
        Enc { out: Vec::new(), cur: 0, nbits: 0, low: 0, high: AC_MAX, pending: 0 }
    }
    #[inline]
    fn bit(&mut self, b: u32) {
        self.cur = (self.cur << 1) | (b & 1);
        self.nbits += 1;
        if self.nbits == 8 {
            self.out.push(self.cur as u8);
            self.cur = 0;
            self.nbits = 0;
        }
    }
    #[inline]
    fn emit(&mut self, bit: u32) {
        self.bit(bit);
        while self.pending > 0 {
            self.bit(bit ^ 1);
            self.pending -= 1;
        }
    }
    fn encode(&mut self, cum: u64, freq: u64, total: u64) {
        let span = self.high - self.low + 1;
        self.high = self.low + span * (cum + freq) / total - 1;
        self.low += span * cum / total;
        loop {
            if self.high < AC_HALF {
                self.emit(0);
            } else if self.low >= AC_HALF {
                self.emit(1);
                self.low -= AC_HALF;
                self.high -= AC_HALF;
            } else if self.low >= AC_QUARTER && self.high < AC_3QUARTER {
                self.pending += 1;
                self.low -= AC_QUARTER;
                self.high -= AC_QUARTER;
            } else {
                break;
            }
            self.low <<= 1;
            self.high = (self.high << 1) | 1;
        }
    }
    fn finish(mut self) -> Vec<u8> {
        self.pending += 1;
        let b = if self.low < AC_QUARTER { 0 } else { 1 };
        self.emit(b);
        if self.nbits > 0 {
            self.out.push((self.cur << (8 - self.nbits)) as u8); // zero-pad final byte
        }
        self.out
    }
}

pub fn encode(res: &[i64]) -> Vec<u8> {
    let mut freq = vec![[1i32; NB]; NCTX];
    let mut tot = vec![NB as i64; NCTX];
    let mut mf = vec![[1i32; 2]; NCTX * NB]; // top mantissa bit | (ctx, k)
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

// --- arithmetic decoder ------------------------------------------------------

struct Dec<'a> {
    inp: &'a [u8],
    pos: usize,
    low: u64,
    high: u64,
    code: u64,
}

impl<'a> Dec<'a> {
    fn new(inp: &'a [u8]) -> Self {
        let mut d = Dec { inp, pos: 0, low: 0, high: AC_MAX, code: 0 };
        for _ in 0..32 {
            d.code = (d.code << 1) | d.read_bit();
        }
        d
    }
    #[inline]
    fn read_bit(&mut self) -> u64 {
        let bi = self.pos >> 3;
        let b = if bi >= self.inp.len() { 0 } else { ((self.inp[bi] >> (7 - (self.pos & 7))) & 1) as u64 };
        self.pos += 1;
        b
    }
    #[inline]
    fn target(&self, total: u64) -> u64 {
        let span = self.high - self.low + 1;
        ((self.code - self.low + 1) * total - 1) / span
    }
    fn update(&mut self, cum: u64, freq: u64, total: u64) {
        let span = self.high - self.low + 1;
        self.high = self.low + span * (cum + freq) / total - 1;
        self.low += span * cum / total;
        loop {
            if self.high < AC_HALF {
            } else if self.low >= AC_HALF {
                self.low -= AC_HALF;
                self.high -= AC_HALF;
                self.code -= AC_HALF;
            } else if self.low >= AC_QUARTER && self.high < AC_3QUARTER {
                self.low -= AC_QUARTER;
                self.high -= AC_QUARTER;
                self.code -= AC_QUARTER;
            } else {
                break;
            }
            self.low <<= 1;
            self.high = (self.high << 1) | 1;
            self.code = (self.code << 1) | self.read_bit();
        }
    }
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
        out.push((u >> 1) as i64 ^ -((u & 1) as i64)); // unzigzag
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

// --- C ABI (drop-in for the ctypes loader) -----------------------------------

/// Encode `n` i64 residuals into `out` (capacity `cap`). Returns bytes written, or -1
/// if it doesn't fit — same contract as the C `ctx_encode`.
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

/// Decode `n` i64 residuals from `input` (length `len`) into `out` — same contract as C.
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
        let b = encode(v);
        assert_eq!(decode(&b, v.len()), v);
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
