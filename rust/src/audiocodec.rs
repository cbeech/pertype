//! Lossless audio codec — byte-identical to `compressor/audiocodec.py` and its C native.
//! mid/side → fixed order-2 → sign-sign LMS cascade → adaptive Rice (or ctxcoder). All
//! integer (wrapping, matching numpy int64 / the C `-fwrapv`); the Rice run-magnitude uses
//! f64 exactly as the reference (no FMA). A blob round-trips and is cross-decodable with
//! Python.

use crate::arith::{unzigzag, zigzag};
use crate::ctxcoder;

const MAGIC: &[u8] = b"AUD1";
const STAGES: [(usize, u32); 3] = [(16, 10), (256, 13), (512, 14)];
const RICE_ALPHA: f64 = 0.02;

#[inline]
fn sgn(v: i64) -> i64 {
    (v > 0) as i64 - (v < 0) as i64
}

// --- predictor cascade -------------------------------------------------------

fn fixed2_fwd(x: &[i64]) -> Vec<i64> {
    let mut e = x.to_vec();
    for i in 2..x.len() {
        e[i] = x[i].wrapping_sub((2i64).wrapping_mul(x[i - 1]).wrapping_sub(x[i - 2]));
    }
    e
}

fn fixed2_inv(e: &[i64]) -> Vec<i64> {
    let mut x = e.to_vec();
    for i in 2..e.len() {
        x[i] = e[i].wrapping_add((2i64).wrapping_mul(x[i - 1]).wrapping_sub(x[i - 2]));
    }
    x
}

fn lms_fwd(x: &[i64], taps: usize, shift: u32) -> Vec<i64> {
    let mut w = vec![0i64; taps];
    let mut h = vec![0i64; taps];
    let mut out = vec![0i64; x.len()];
    for i in 0..x.len() {
        let mut sum = 0i64;
        for j in 0..taps {
            sum = sum.wrapping_add(w[j].wrapping_mul(h[j]));
        }
        let pred = sum >> shift; // arithmetic shift = floor
        let err = x[i].wrapping_sub(pred);
        out[i] = err;
        if err > 0 {
            for j in 0..taps {
                w[j] += sgn(h[j]);
            }
        } else if err < 0 {
            for j in 0..taps {
                w[j] -= sgn(h[j]);
            }
        }
        for j in (1..taps).rev() {
            h[j] = h[j - 1];
        }
        h[0] = x[i];
    }
    out
}

fn lms_inv(e: &[i64], taps: usize, shift: u32) -> Vec<i64> {
    let mut w = vec![0i64; taps];
    let mut h = vec![0i64; taps];
    let mut x = vec![0i64; e.len()];
    for i in 0..e.len() {
        let mut sum = 0i64;
        for j in 0..taps {
            sum = sum.wrapping_add(w[j].wrapping_mul(h[j]));
        }
        let pred = sum >> shift;
        let xi = e[i].wrapping_add(pred);
        x[i] = xi;
        if e[i] > 0 {
            for j in 0..taps {
                w[j] += sgn(h[j]);
            }
        } else if e[i] < 0 {
            for j in 0..taps {
                w[j] -= sgn(h[j]);
            }
        }
        for j in (1..taps).rev() {
            h[j] = h[j - 1];
        }
        h[0] = xi;
    }
    x
}

fn predict_fwd(x: &[i64]) -> Vec<i64> {
    let mut e = fixed2_fwd(x);
    for &(taps, shift) in &STAGES {
        e = lms_fwd(&e, taps, shift);
    }
    e
}

fn predict_inv(e: &[i64]) -> Vec<i64> {
    let mut e = e.to_vec();
    for &(taps, shift) in STAGES.iter().rev() {
        e = lms_inv(&e, taps, shift);
    }
    fixed2_inv(&e)
}

// --- adaptive Rice -----------------------------------------------------------

fn k_from_run(run: f64) -> u32 {
    let v = run as i64; // trunc toward zero
    if v < 1 {
        0
    } else {
        63 - (v as u64).leading_zeros()
    }
}

fn rice_encode(res: &[i64]) -> Vec<u8> {
    let mut out = Vec::new();
    let mut cur: u32 = 0;
    let mut nbits = 0u32;
    let mut run = 16.0f64;
    let put = |bit: u32, out: &mut Vec<u8>, cur: &mut u32, nbits: &mut u32| {
        *cur = (*cur << 1) | (bit & 1);
        *nbits += 1;
        if *nbits == 8 {
            out.push(*cur as u8);
            *cur = 0;
            *nbits = 0;
        }
    };
    for &r in res {
        let u = zigzag(r);
        let k = k_from_run(run);
        let q = u >> k;
        for t in 0..q + 1 {
            put(if t < q { 1 } else { 0 }, &mut out, &mut cur, &mut nbits);
        }
        for s in (0..k).rev() {
            put(((u >> s) & 1) as u32, &mut out, &mut cur, &mut nbits);
        }
        run += (u as f64 - run) * RICE_ALPHA;
    }
    if nbits > 0 {
        out.push((cur << (8 - nbits)) as u8);
    }
    out
}

fn rice_decode(inp: &[u8], n: usize) -> Vec<i64> {
    let mut out = vec![0i64; n];
    let mut pos = 0usize;
    let mut run = 16.0f64;
    let bit = |pos: usize| -> u64 {
        if pos >> 3 >= inp.len() {
            0
        } else {
            ((inp[pos >> 3] >> (7 - (pos & 7))) & 1) as u64
        }
    };
    for i in 0..n {
        let k = k_from_run(run);
        let mut q = 0u64;
        while bit(pos) == 1 {
            q += 1;
            pos += 1;
        }
        pos += 1; // terminating zero
        let mut rem = 0u64;
        for _ in 0..k {
            rem = (rem << 1) | bit(pos);
            pos += 1;
        }
        let u = (q << k) | rem;
        out[i] = unzigzag(u);
        run += (u as f64 - run) * RICE_ALPHA;
    }
    out
}

fn res_encode(res: &[i64], coder: u8) -> Vec<u8> {
    if coder == 1 {
        ctxcoder::encode(res)
    } else {
        rice_encode(res)
    }
}

fn res_decode(blob: &[u8], n: usize, coder: u8) -> Vec<i64> {
    if coder == 1 {
        ctxcoder::decode(blob, n)
    } else {
        rice_decode(blob, n)
    }
}

// --- mid/side + container ----------------------------------------------------

fn u32be(x: usize) -> [u8; 4] {
    (x as u32).to_be_bytes()
}
fn u64be(x: usize) -> [u8; 8] {
    (x as u64).to_be_bytes()
}

/// `pcm`: interleaved i16 samples, `n` frames × `channels`. Returns an AUD1 blob.
pub fn encode(pcm: &[i16], n: usize, channels: usize, samplerate: u32, coder: u8) -> Vec<u8> {
    let streams: Vec<Vec<i64>> = if channels == 2 {
        let mut m = vec![0i64; n];
        let mut s = vec![0i64; n];
        for i in 0..n {
            let (l, r) = (pcm[2 * i] as i64, pcm[2 * i + 1] as i64);
            s[i] = l - r;
            m[i] = r + (s[i] >> 1);
        }
        vec![m, s]
    } else {
        (0..channels)
            .map(|c| (0..n).map(|i| pcm[i * channels + c] as i64).collect())
            .collect()
    };
    let mut out = Vec::new();
    out.extend_from_slice(MAGIC);
    out.push(coder);
    out.push(channels as u8);
    out.extend_from_slice(&u32be(samplerate as usize));
    out.extend_from_slice(&u64be(n));
    for st in &streams {
        let blob = res_encode(&predict_fwd(st), coder);
        out.extend_from_slice(&u32be(blob.len()));
        out.extend_from_slice(&blob);
    }
    out
}

/// Returns (interleaved i16 pcm, samplerate).
pub fn decode(blob: &[u8]) -> (Vec<i16>, u32) {
    assert_eq!(&blob[..4], MAGIC, "not an AUD1 stream");
    let coder = blob[4];
    let channels = blob[5] as usize;
    let samplerate = u32::from_be_bytes([blob[6], blob[7], blob[8], blob[9]]);
    let n = u64::from_be_bytes([blob[10], blob[11], blob[12], blob[13], blob[14], blob[15],
                               blob[16], blob[17]]) as usize;
    let mut p = 18;
    let mut streams = Vec::with_capacity(channels);
    for _ in 0..channels {
        let ln = u32::from_be_bytes([blob[p], blob[p + 1], blob[p + 2], blob[p + 3]]) as usize;
        p += 4;
        streams.push(predict_inv(&res_decode(&blob[p..p + ln], n, coder)));
        p += ln;
    }
    let mut pcm = vec![0i16; n * channels];
    if channels == 2 {
        for i in 0..n {
            let (m, s) = (streams[0][i], streams[1][i]);
            let r = m - (s >> 1);
            let l = r + s;
            pcm[2 * i] = l as i16;
            pcm[2 * i + 1] = r as i16;
        }
    } else {
        for c in 0..channels {
            for i in 0..n {
                pcm[i * channels + c] = streams[c][i] as i16;
            }
        }
    }
    (pcm, samplerate)
}

#[no_mangle]
pub unsafe extern "C" fn audio_encode(
    pcm: *const i16, n: i64, channels: i64, samplerate: i64, coder: i64, out: *mut u8, cap: i64,
) -> i64 {
    let pcm = std::slice::from_raw_parts(pcm, (n * channels) as usize);
    let blob = encode(pcm, n as usize, channels as usize, samplerate as u32, coder as u8);
    if blob.len() as i64 > cap {
        return -1;
    }
    std::ptr::copy_nonoverlapping(blob.as_ptr(), out, blob.len());
    blob.len() as i64
}

#[no_mangle]
pub unsafe extern "C" fn audio_decode(input: *const u8, len: i64, out: *mut i16, cap: i64) -> i64 {
    let (pcm, _sr) = decode(std::slice::from_raw_parts(input, len as usize));
    if pcm.len() as i64 > cap {
        return -1;
    }
    std::ptr::copy_nonoverlapping(pcm.as_ptr(), out, pcm.len());
    pcm.len() as i64
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn roundtrip() {
        let n = 8000usize;
        let mut pcm = vec![0i16; n * 2];
        let mut s: u64 = 11;
        let (mut l, mut r) = (0i32, 0i32);
        for i in 0..n {
            s = s.wrapping_mul(6364136223846793005).wrapping_add(1);
            l = (l + (s >> 60) as i32 - 8).clamp(-30000, 30000);
            r = (r + ((s >> 56) & 15) as i32 - 8).clamp(-30000, 30000);
            pcm[2 * i] = l as i16;
            pcm[2 * i + 1] = r as i16;
        }
        for coder in [0u8, 1] {
            let (d, _) = decode(&encode(&pcm, n, 2, 44100, coder));
            assert_eq!(d, pcm);
        }
    }
}
