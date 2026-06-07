//! Reversible byte-stream transforms — byte-identical to `compressor/transform.py`
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
