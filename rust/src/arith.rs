//! Shared Witten–Neal–Cleary 32-bit arithmetic coder + bit I/O, and the bucket-model
//! constants used by both the ctxcoder and CALIC ports. MSB-first output with a
//! zero-padded final byte, byte-identical to `pertype/_native/audio.c`.

pub const NB: usize = 65; // magnitude buckets 0..64 cover any int64 zigzag
pub const INCR: i64 = 32;
pub const RESCALE: i64 = 1 << 14;
pub const MINCR: i32 = 24; // adaptation of the modelled top-mantissa bit
pub const MRESCALE: i64 = 1 << 13;
pub const AC_HALF: u64 = 0x8000_0000;
pub const AC_QUARTER: u64 = 0x4000_0000;
pub const AC_3QUARTER: u64 = 0xC000_0000;
pub const AC_MAX: u64 = 0xFFFF_FFFF;

#[inline]
pub fn zigzag(r: i64) -> u64 {
    ((r as u64) << 1) ^ ((r >> 63) as u64)
}

#[inline]
pub fn unzigzag(u: u64) -> i64 {
    (u >> 1) as i64 ^ -((u & 1) as i64)
}

#[inline]
pub fn bit_length(u: u64) -> usize {
    if u == 0 { 0 } else { 64 - u.leading_zeros() as usize }
}

/// Arithmetic encoder writing MSB-first bits into a growing byte buffer.
pub struct Enc {
    out: Vec<u8>,
    cur: u32,
    nbits: u32,
    low: u64,
    high: u64,
    pending: i64,
}

impl Enc {
    pub fn new() -> Self {
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
    pub fn encode(&mut self, cum: u64, freq: u64, total: u64) {
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
    pub fn finish(mut self) -> Vec<u8> {
        self.pending += 1;
        let b = if self.low < AC_QUARTER { 0 } else { 1 };
        self.emit(b);
        if self.nbits > 0 {
            self.out.push((self.cur << (8 - self.nbits)) as u8);
        }
        self.out
    }
}

/// Arithmetic decoder reading MSB-first bits (0 past end of input).
pub struct Dec<'a> {
    inp: &'a [u8],
    pos: usize,
    low: u64,
    high: u64,
    code: u64,
}

impl<'a> Dec<'a> {
    pub fn new(inp: &'a [u8]) -> Self {
        let mut d = Dec { inp, pos: 0, low: 0, high: AC_MAX, code: 0 };
        for _ in 0..32 {
            d.code = (d.code << 1) | d.read_bit();
        }
        d
    }
    #[inline]
    fn read_bit(&mut self) -> u64 {
        let bi = self.pos >> 3;
        let b = if bi >= self.inp.len() {
            0
        } else {
            ((self.inp[bi] >> (7 - (self.pos & 7))) & 1) as u64
        };
        self.pos += 1;
        b
    }
    #[inline]
    pub fn target(&self, total: u64) -> u64 {
        let span = self.high - self.low + 1;
        ((self.code - self.low + 1) * total - 1) / span
    }
    pub fn update(&mut self, cum: u64, freq: u64, total: u64) {
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
