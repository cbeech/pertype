//! FASTQ quality-score context coder — byte-identical to `pertype/qualcodec.py`
//! and the C twin (`pertype/_native/audio.c`). Adaptive K=94 symbol model per
//! (prev-quality, position-bucket) context on the shared WNC coder; encoder and
//! decoder evolve their counts identically, so nothing is transmitted. The caller
//! passes the read-length array; the within-read position is rebuilt from it.

use crate::arith::*;

pub const K: usize = 94; // Phred symbols: raw ASCII 33..126
pub const QINCR: i64 = 24;
pub const QRESCALE: i64 = 1 << 14;
const POSCAP: i64 = 63; // within-read position clamp (context bucket)
const NCTX: usize = 128 * 64; // prevq (0 sentinel | 33..126) x position bucket

pub fn encode_payload(q: &[u8], lens: &[i32]) -> Vec<u8> {
    let mut cnt = vec![[1i32; K]; NCTX];
    let mut tot = vec![K as i64; NCTX];
    let mut e = Enc::new();
    let mut i = 0usize;
    let mut prevq = 0i64;
    for &l in lens {
        for p in 0..l as i64 {
            let ctx = (prevq * 64 + p.min(POSCAP)) as usize;
            let a = &mut cnt[ctx];
            let s = (q[i] - 33) as usize;
            let mut cum = 0u64;
            for j in 0..s {
                cum += a[j] as u64;
            }
            e.encode(cum, a[s] as u64, tot[ctx] as u64);
            a[s] += QINCR as i32;
            tot[ctx] += QINCR;
            if tot[ctx] >= QRESCALE {
                let mut t = 0i64;
                for j in 0..K {
                    a[j] = (a[j] + 1) >> 1;
                    t += a[j] as i64;
                }
                tot[ctx] = t;
            }
            prevq = s as i64 + 33;
            i += 1;
        }
        prevq = 0; // start-of-read sentinel
    }
    e.finish()
}

pub fn decode_payload(inp: &[u8], lens: &[i32]) -> Vec<u8> {
    let mut cnt = vec![[1i32; K]; NCTX];
    let mut tot = vec![K as i64; NCTX];
    let mut d = Dec::new(inp);
    let mut out = Vec::new();
    let mut prevq = 0i64;
    for &l in lens {
        for p in 0..l as i64 {
            let ctx = (prevq * 64 + p.min(POSCAP)) as usize;
            let a = &mut cnt[ctx];
            let total = tot[ctx] as u64;
            let target = d.target(total);
            let mut cum = 0u64;
            let mut s = 0usize;
            while cum + a[s] as u64 <= target {
                cum += a[s] as u64;
                s += 1;
            }
            d.update(cum, a[s] as u64, total);
            a[s] += QINCR as i32;
            tot[ctx] += QINCR;
            if tot[ctx] >= QRESCALE {
                let mut t = 0i64;
                for j in 0..K {
                    a[j] = (a[j] + 1) >> 1;
                    t += a[j] as i64;
                }
                tot[ctx] = t;
            }
            out.push((s + 33) as u8);
            prevq = s as i64 + 33;
        }
        prevq = 0;
    }
    out
}

#[no_mangle]
pub unsafe extern "C" fn qual_encode(q: *const u8, n: i64, lens: *const i32,
                                     nreads: i64, out: *mut u8, cap: i64) -> i64 {
    let q = std::slice::from_raw_parts(q, n as usize);
    let lens = std::slice::from_raw_parts(lens, nreads as usize);
    let bytes = encode_payload(q, lens);
    if bytes.len() as i64 > cap {
        return -1;
    }
    std::ptr::copy_nonoverlapping(bytes.as_ptr(), out, bytes.len());
    bytes.len() as i64
}

#[no_mangle]
pub unsafe extern "C" fn qual_decode(input: *const u8, len: i64, _n: i64,
                                     lens: *const i32, nreads: i64, out: *mut u8) {
    let inp = std::slice::from_raw_parts(input, len as usize);
    let lens = std::slice::from_raw_parts(lens, nreads as usize);
    let vals = decode_payload(inp, lens);
    std::ptr::copy_nonoverlapping(vals.as_ptr(), out, vals.len());
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rt(reads: &[Vec<u8>]) {
        let q: Vec<u8> = reads.concat();
        let lens: Vec<i32> = reads.iter().map(|r| r.len() as i32).collect();
        assert_eq!(decode_payload(&encode_payload(&q, &lens), &lens), q);
    }

    #[test]
    fn roundtrip() {
        rt(&[]);
        rt(&[b"I".repeat(200), b"!".repeat(1), b"~".repeat(64), vec![73u8; 63]]);
        let mut s: u64 = 0x1234_5678;
        let mut reads = Vec::new();
        for _ in 0..200 {
            let len = (s >> 33) as usize % 200 + 1;
            s = s.wrapping_mul(6364136223846793005).wrapping_add(1);
            let mut q = 40i32;
            let mut read = Vec::new();
            for _ in 0..len {
                s = s.wrapping_mul(6364136223846793005).wrapping_add(1);
                q = (q + (s >> 60) as i32 % 5 - 2).clamp(33, 126);
                read.push(q as u8);
            }
            reads.push(read);
        }
        rt(&reads);
    }
}
