//! Lossless video codec — motion-compensated inter-frame coding, byte-identical to
//! `pertype/videocodec.py`. Per 16×16 block, per frame (frame 0 all-intra):
//!   * SKIP  — block bit-identical to the co-located previous block (mode flag only).
//!   * INTER — quarter-pel motion-compensated residual vs the previous frame (+ luma MV).
//!   * INTRA — residual vs the causal MED (JPEG-LS) predictor in this frame.
//! Mode field, motion vectors (inter only) and residuals (non-skip) are `ctxcoder`-coded.
//! All integer arithmetic mirrors numpy's int64 (no wrap occurs — values stay small), and
//! the tie-breaking in every search matches numpy's strict `<` (keep earlier candidate).

use crate::arith::{bit_length, zigzag};
use crate::ctxcoder;
use crate::predictors;

const MAGIC: &[u8] = b"VID1";
const MAGIC_YUV: &[u8] = b"VYUV";
const B: usize = 16; // block size
const S: i64 = 8; // coarse integer search radius (at the ÷2 level)
const R: i64 = 4; // full-res integer refinement radius

#[inline]
fn clip(v: i64, hi: i64) -> usize {
    if v < 0 {
        0
    } else if v > hi {
        hi as usize
    } else {
        v as usize
    }
}

// --- ÷2 box downsample (matches `(p00+p10+p01+p11) >> 2`) --------------------

fn box_down(a: &[i64], h: usize, w: usize) -> (Vec<i64>, usize, usize) {
    let (h2, w2) = (h / 2, w / 2);
    let mut out = vec![0i64; h2 * w2];
    for y in 0..h2 {
        for x in 0..w2 {
            let (yy, xx) = (2 * y, 2 * x);
            out[y * w2 + x] = (a[yy * w + xx]
                + a[(yy + 1) * w + xx]
                + a[yy * w + xx + 1]
                + a[(yy + 1) * w + xx + 1])
                >> 2;
        }
    }
    (out, h2, w2)
}

// --- lockstep full integer search (coarse level) ----------------------------

/// Per `blk`×`blk` block, the (dy,dx) in [-radius,radius]² minimising SAD vs `prev`,
/// with clamped shifts. Ties keep the earlier (dy outer, dx inner) candidate.
fn full_search(
    prev: &[i64], curr: &[i64], h: usize, w: usize, radius: i64, blk: usize,
) -> (Vec<i64>, Vec<i64>) {
    let (nby, nbx) = (h / blk, w / blk);
    let mut best = vec![1i64 << 30; nby * nbx];
    let mut bdy = vec![0i64; nby * nbx];
    let mut bdx = vec![0i64; nby * nbx];
    let hi_y = h as i64 - 1;
    let hi_x = w as i64 - 1;
    for dy in -radius..=radius {
        for dx in -radius..=radius {
            for by in 0..nby {
                for bx in 0..nbx {
                    let mut sad = 0i64;
                    for i in 0..blk {
                        let y = by * blk + i;
                        let py = clip(y as i64 + dy, hi_y);
                        for j in 0..blk {
                            let x = bx * blk + j;
                            let px = clip(x as i64 + dx, hi_x);
                            sad += (curr[y * w + x] - prev[py * w + px]).abs();
                        }
                    }
                    let bi = by * nbx + bx;
                    if sad < best[bi] {
                        best[bi] = sad;
                        bdy[bi] = dy;
                        bdx[bi] = dx;
                    }
                }
            }
        }
    }
    (bdy, bdx)
}

// --- per-block integer-MV SAD (clamped gather) ------------------------------

fn sad_int(
    prev: &[i64], curr: &[i64], h: usize, w: usize, mvy: &[i64], mvx: &[i64],
) -> Vec<i64> {
    let (nby, nbx) = (h / B, w / B);
    let (hi_y, hi_x) = (h as i64 - 1, w as i64 - 1);
    let mut sad = vec![0i64; nby * nbx];
    for by in 0..nby {
        for bx in 0..nbx {
            let bi = by * nbx + bx;
            let (my, mx) = (mvy[bi], mvx[bi]);
            let mut s = 0i64;
            for i in 0..B {
                let y = by * B + i;
                let py = clip(y as i64 + my, hi_y);
                for j in 0..B {
                    let x = bx * B + j;
                    let px = clip(x as i64 + mx, hi_x);
                    s += (curr[y * w + x] - prev[py * w + px]).abs();
                }
            }
            sad[bi] = s;
        }
    }
    sad
}

// --- hierarchical motion estimate -------------------------------------------

fn motion_estimate(
    prev: &[i64], curr: &[i64], h: usize, w: usize,
) -> (Vec<i64>, Vec<i64>) {
    let (pc, h2, w2) = box_down(prev, h, w);
    let (cc, _, _) = box_down(curr, h, w);
    let (cby, cbx) = full_search(&pc, &cc, h2, w2, S, B / 2);
    let (nby, nbx) = (h / B, w / B);
    let base_y: Vec<i64> = cby.iter().map(|&v| v * 2).collect();
    let base_x: Vec<i64> = cbx.iter().map(|&v| v * 2).collect();

    let mut best = vec![1i64 << 30; nby * nbx];
    let mut by = base_y.clone();
    let mut bx = base_x.clone();
    for ddy in -R..=R {
        for ddx in -R..=R {
            let mvy: Vec<i64> = base_y.iter().map(|&v| v + ddy).collect();
            let mvx: Vec<i64> = base_x.iter().map(|&v| v + ddx).collect();
            let sad = sad_int(prev, curr, h, w, &mvy, &mvx);
            for k in 0..nby * nbx {
                if sad[k] < best[k] {
                    best[k] = sad[k];
                    by[k] = mvy[k];
                    bx[k] = mvx[k];
                }
            }
        }
    }
    let zeros = vec![0i64; nby * nbx];
    let sad0 = sad_int(prev, curr, h, w, &zeros, &zeros);
    for k in 0..nby * nbx {
        if sad0[k] <= best[k] {
            by[k] = 0;
            bx[k] = 0;
        }
    }
    (by, bx)
}

// --- quarter-pel bilinear prediction ----------------------------------------

fn predict_qpel(p: &[i64], h: usize, w: usize, mvy: &[i64], mvx: &[i64]) -> Vec<i64> {
    let (nby, nbx) = (h / B, w / B);
    let (hi_y, hi_x) = (h as i64 - 1, w as i64 - 1);
    let mut out = vec![0i64; h * w];
    for by in 0..nby {
        for bx in 0..nbx {
            let bi = by * nbx + bx;
            let (my, mx) = (mvy[bi], mvx[bi]);
            let (iy, ry) = (my >> 2, my & 3);
            let (ix, rx) = (mx >> 2, mx & 3);
            let (w00, w01) = ((4 - ry) * (4 - rx), (4 - ry) * rx);
            let (w10, w11) = (ry * (4 - rx), ry * rx);
            for i in 0..B {
                let y = by * B + i;
                let yy = clip(y as i64 + iy, hi_y);
                let yp = clip(y as i64 + iy + 1, hi_y);
                for j in 0..B {
                    let x = bx * B + j;
                    let xx = clip(x as i64 + ix, hi_x);
                    let xp = clip(x as i64 + ix + 1, hi_x);
                    out[y * w + x] = (w00 * p[yy * w + xx]
                        + w01 * p[yy * w + xp]
                        + w10 * p[yp * w + xx]
                        + w11 * p[yp * w + xp]
                        + 8)
                        >> 4;
                }
            }
        }
    }
    out
}

// --- block SAD vs a qpel prediction (used by the sub-pel refine) -------------

fn block_sad_qpel(
    prev: &[i64], curr: &[i64], h: usize, w: usize, mvy: &[i64], mvx: &[i64],
) -> Vec<i64> {
    let pred = predict_qpel(prev, h, w, mvy, mvx);
    let (nby, nbx) = (h / B, w / B);
    let mut sad = vec![0i64; nby * nbx];
    for by in 0..nby {
        for bx in 0..nbx {
            let mut s = 0i64;
            for i in 0..B {
                let y = by * B + i;
                for j in 0..B {
                    let x = bx * B + j;
                    s += (curr[y * w + x] - pred[y * w + x]).abs();
                }
            }
            sad[by * nbx + bx] = s;
        }
    }
    sad
}

fn refine(
    prev: &[i64], curr: &[i64], h: usize, w: usize, bdy: &[i64], bdx: &[i64],
) -> (Vec<i64>, Vec<i64>) {
    let (nby, nbx) = (h / B, w / B);
    let search = |base_y: &[i64], base_x: &[i64], steps: &[i64]| -> (Vec<i64>, Vec<i64>) {
        let mut best = vec![1i64 << 30; nby * nbx];
        let mut by = base_y.to_vec();
        let mut bx = base_x.to_vec();
        for &ddy in steps {
            for &ddx in steps {
                let mvy: Vec<i64> = base_y.iter().map(|&v| v + ddy).collect();
                let mvx: Vec<i64> = base_x.iter().map(|&v| v + ddx).collect();
                let sad = block_sad_qpel(prev, curr, h, w, &mvy, &mvx);
                for k in 0..nby * nbx {
                    if sad[k] < best[k] {
                        best[k] = sad[k];
                        by[k] = mvy[k];
                        bx[k] = mvx[k];
                    }
                }
            }
        }
        (by, bx)
    };
    let h4y: Vec<i64> = bdy.iter().map(|&v| 4 * v).collect();
    let h4x: Vec<i64> = bdx.iter().map(|&v| 4 * v).collect();
    let (hy, hx) = search(&h4y, &h4x, &[0, -2, 2]);
    search(&hy, &hx, &[0, -1, 1])
}

// --- per-block residual bit cost --------------------------------------------

fn block_cost(res: &[i64], h: usize, w: usize) -> Vec<i64> {
    let (nby, nbx) = (h / B, w / B);
    let mut cost = vec![0i64; nby * nbx];
    for by in 0..nby {
        for bx in 0..nbx {
            let mut c = 0i64;
            for i in 0..B {
                let y = by * B + i;
                for j in 0..B {
                    let x = bx * B + j;
                    c += bit_length(zigzag(res[y * w + x])) as i64;
                }
            }
            cost[by * nbx + bx] = c;
        }
    }
    cost
}

// --- causal MED reconstruction (mirrors `_med_fill`) ------------------------

fn med_fill(rec: &mut [i64], intra: &[bool], residual: &[i64], h: usize, w: usize) {
    for y in 0..h {
        for x in 0..w {
            let i = y * w + x;
            if !intra[i] {
                continue;
            }
            let a = if x > 0 {
                rec[i - 1]
            } else if y > 0 {
                rec[i - w]
            } else {
                128
            };
            let b = if y > 0 { rec[i - w] } else { a };
            let c = if x > 0 && y > 0 { rec[i - w - 1] } else { b };
            let (mx, mn) = if a > b { (a, b) } else { (b, a) };
            let pred = if c >= mx {
                mn
            } else if c <= mn {
                mx
            } else {
                a + b - c
            };
            rec[i] = pred + residual[i];
        }
    }
}

// --- block (de)interleave for non-skip residuals ----------------------------

fn to_blocks(sel: &[i64], keep: &[bool], h: usize, w: usize) -> Vec<i64> {
    let (nby, nbx) = (h / B, w / B);
    let mut out = Vec::new();
    for by in 0..nby {
        for bx in 0..nbx {
            if !keep[by * nbx + bx] {
                continue;
            }
            for i in 0..B {
                let y = by * B + i;
                for j in 0..B {
                    out.push(sel[y * w + (bx * B + j)]);
                }
            }
        }
    }
    out
}

fn from_blocks(values: &[i64], keep: &[bool], h: usize, w: usize) -> Vec<i64> {
    let (nby, nbx) = (h / B, w / B);
    let mut out = vec![0i64; h * w];
    let mut p = 0usize;
    for by in 0..nby {
        for bx in 0..nbx {
            if !keep[by * nbx + bx] {
                continue;
            }
            for i in 0..B {
                let y = by * B + i;
                for j in 0..B {
                    out[y * w + (bx * B + j)] = values[p];
                    p += 1;
                }
            }
        }
    }
    out
}

// --- container helpers ------------------------------------------------------

fn put(out: &mut Vec<u8>, blob: &[u8]) {
    out.extend_from_slice(&(blob.len() as u32).to_be_bytes());
    out.extend_from_slice(blob);
}

fn take<'a>(blob: &'a [u8], pos: &mut usize) -> &'a [u8] {
    let n = u32::from_be_bytes([blob[*pos], blob[*pos + 1], blob[*pos + 2], blob[*pos + 3]])
        as usize;
    let s = &blob[*pos + 4..*pos + 4 + n];
    *pos += 4 + n;
    s
}

// --- per-block mode decision ------------------------------------------------

struct Modes {
    mode: Vec<i64>,    // nby*nbx: 0=inter 1=intra 2=skip
    mvy: Vec<i64>,     // nby*nbx
    mvx: Vec<i64>,     // nby*nbx
    sel: Vec<i64>,     // h*w chosen residual
    skip: Vec<bool>,   // nby*nbx
    inter: Vec<bool>,  // nby*nbx
}

fn choose_modes(prev: &[i64], cur: &[i64], h: usize, w: usize) -> Modes {
    let (nby, nbx) = (h / B, w / B);
    let (bdy, bdx) = motion_estimate(prev, cur, h, w);
    let (mvy, mvx) = refine(prev, cur, h, w, &bdy, &bdx);
    let mc = predict_qpel(prev, h, w, &mvy, &mvx);
    let inter_res: Vec<i64> = (0..h * w).map(|i| cur[i] - mc[i]).collect();
    let intra_res = predictors::med_residual(cur, h, w);

    let cost_intra = block_cost(&intra_res, h, w);
    let cost_inter = block_cost(&inter_res, h, w);

    let mut skip = vec![false; nby * nbx];
    for by in 0..nby {
        for bx in 0..nbx {
            let mut all_zero = true;
            'blk: for i in 0..B {
                let y = by * B + i;
                for j in 0..B {
                    let x = bx * B + j;
                    if cur[y * w + x] != prev[y * w + x] {
                        all_zero = false;
                        break 'blk;
                    }
                }
            }
            skip[by * nbx + bx] = all_zero;
        }
    }

    let mut mode = vec![0i64; nby * nbx];
    let mut inter = vec![false; nby * nbx];
    let mut intra_blk = vec![false; nby * nbx];
    for k in 0..nby * nbx {
        let use_intra = cost_intra[k] < cost_inter[k];
        intra_blk[k] = use_intra && !skip[k];
        inter[k] = !use_intra && !skip[k];
        if intra_blk[k] {
            mode[k] = 1;
        }
        if skip[k] {
            mode[k] = 2;
        }
    }

    let mut sel = vec![0i64; h * w];
    for by in 0..nby {
        for bx in 0..nbx {
            let is_intra = intra_blk[by * nbx + bx];
            for i in 0..B {
                let y = by * B + i;
                for j in 0..B {
                    let x = bx * B + j;
                    sel[y * w + x] = if is_intra { intra_res[y * w + x] } else { inter_res[y * w + x] };
                }
            }
        }
    }
    Modes { mode, mvy, mvx, sel, skip, inter }
}

// --- single-plane encode / decode -------------------------------------------

/// `frames`: T·H·W row-major u8 (H,W multiples of 16). Returns a VID1 container.
pub fn encode(frames: &[u8], t: usize, h: usize, w: usize) -> Vec<u8> {
    assert!(h % B == 0 && w % B == 0, "frame dims must be multiples of 16");
    let plane = |k: usize| -> Vec<i64> {
        frames[k * h * w..(k + 1) * h * w].iter().map(|&v| v as i64).collect()
    };
    let mut out = Vec::new();
    out.extend_from_slice(MAGIC);
    out.extend_from_slice(&(t as u32).to_be_bytes());
    out.extend_from_slice(&(h as u32).to_be_bytes());
    out.extend_from_slice(&(w as u32).to_be_bytes());

    let f0 = plane(0);
    put(&mut out, &ctxcoder::encode(&predictors::med_residual(&f0, h, w)));

    let (nby, nbx) = (h / B, w / B);
    let mut prev = f0;
    for tt in 1..t {
        let cur = plane(tt);
        let m = choose_modes(&prev, &cur, h, w);
        put(&mut out, &ctxcoder::encode(&m.mode));
        let mut mv = Vec::new();
        for k in 0..nby * nbx {
            if m.inter[k] {
                mv.push(m.mvy[k]);
            }
        }
        for k in 0..nby * nbx {
            if m.inter[k] {
                mv.push(m.mvx[k]);
            }
        }
        put(&mut out, &ctxcoder::encode(&mv));
        let keep: Vec<bool> = m.skip.iter().map(|&s| !s).collect();
        put(&mut out, &ctxcoder::encode(&to_blocks(&m.sel, &keep, h, w)));
        prev = cur;
    }
    out
}

/// Returns (T·H·W row-major u8, T, H, W).
pub fn decode(blob: &[u8]) -> (Vec<u8>, usize, usize, usize) {
    assert_eq!(&blob[..4], MAGIC, "not a VID1 stream");
    let t = u32::from_be_bytes([blob[4], blob[5], blob[6], blob[7]]) as usize;
    let h = u32::from_be_bytes([blob[8], blob[9], blob[10], blob[11]]) as usize;
    let w = u32::from_be_bytes([blob[12], blob[13], blob[14], blob[15]]) as usize;
    let mut pos = 16usize;
    let (nby, nbx) = (h / B, w / B);

    let b0 = take(blob, &mut pos);
    let res0 = ctxcoder::decode(b0, h * w);
    let mut rec = vec![0i64; h * w];
    let all_intra = vec![true; h * w];
    med_fill(&mut rec, &all_intra, &res0, h, w);

    let mut frames = vec![0u8; t * h * w];
    for i in 0..h * w {
        frames[i] = rec[i] as u8;
    }
    let mut prev = rec;

    for tt in 1..t {
        let mb = take(blob, &mut pos);
        let mode = ctxcoder::decode(mb, nby * nbx);
        let skip: Vec<bool> = mode.iter().map(|&m| m == 2).collect();
        let intra_blk: Vec<bool> = mode.iter().map(|&m| m == 1).collect();
        let inter: Vec<bool> = mode.iter().map(|&m| m == 0).collect();
        let n_inter = inter.iter().filter(|&&b| b).count();
        let n_nonskip = skip.iter().filter(|&&b| !b).count();

        let vb = take(blob, &mut pos);
        let mv = ctxcoder::decode(vb, 2 * n_inter);
        let rb = take(blob, &mut pos);
        let res = ctxcoder::decode(rb, n_nonskip * B * B);

        let mut mvy = vec![0i64; nby * nbx];
        let mut mvx = vec![0i64; nby * nbx];
        let mut p = 0;
        for k in 0..nby * nbx {
            if inter[k] {
                mvy[k] = mv[p];
                p += 1;
            }
        }
        for k in 0..nby * nbx {
            if inter[k] {
                mvx[k] = mv[p];
                p += 1;
            }
        }
        let mc = predict_qpel(&prev, h, w, &mvy, &mvx);
        let keep: Vec<bool> = skip.iter().map(|&s| !s).collect();
        let residual = from_blocks(&res, &keep, h, w);

        let mut rec = vec![0i64; h * w];
        let mut intra_px = vec![false; h * w];
        for by in 0..nby {
            for bx in 0..nbx {
                let bi = by * nbx + bx;
                for i in 0..B {
                    let y = by * B + i;
                    for j in 0..B {
                        let x = bx * B + j;
                        let idx = y * w + x;
                        if skip[bi] {
                            rec[idx] = prev[idx];
                        } else if intra_blk[bi] {
                            intra_px[idx] = true;
                            rec[idx] = -(1 << 30);
                        } else {
                            rec[idx] = mc[idx] + residual[idx];
                        }
                    }
                }
            }
        }
        med_fill(&mut rec, &intra_px, &residual, h, w);
        for i in 0..h * w {
            frames[tt * h * w + i] = rec[i] as u8;
        }
        prev = rec;
    }
    (frames, t, h, w)
}

// --- planar (YUV) -----------------------------------------------------------

/// Encode several independent planes, each T·H·W u8 with its own (t,h,w).
pub fn encode_yuv(planes: &[(&[u8], usize, usize, usize)]) -> Vec<u8> {
    let mut out = Vec::new();
    out.extend_from_slice(MAGIC_YUV);
    out.push(planes.len() as u8);
    for &(p, t, h, w) in planes {
        let blob = encode(p, t, h, w);
        out.extend_from_slice(&(blob.len() as u64).to_be_bytes());
        out.extend_from_slice(&blob);
    }
    out
}

pub fn decode_yuv(blob: &[u8]) -> Vec<(Vec<u8>, usize, usize, usize)> {
    assert_eq!(&blob[..4], MAGIC_YUV, "not a VYUV stream");
    let n = blob[4] as usize;
    let mut pos = 5usize;
    let mut planes = Vec::with_capacity(n);
    for _ in 0..n {
        let ln = u64::from_be_bytes([
            blob[pos], blob[pos + 1], blob[pos + 2], blob[pos + 3], blob[pos + 4],
            blob[pos + 5], blob[pos + 6], blob[pos + 7],
        ]) as usize;
        pos += 8;
        planes.push(decode(&blob[pos..pos + ln]));
        pos += ln;
    }
    planes
}

// --- C ABI ------------------------------------------------------------------

#[no_mangle]
pub unsafe extern "C" fn video_encode(
    frames: *const u8, t: i64, h: i64, w: i64, out: *mut u8, cap: i64,
) -> i64 {
    let frames = std::slice::from_raw_parts(frames, (t * h * w) as usize);
    let blob = encode(frames, t as usize, h as usize, w as usize);
    if blob.len() as i64 > cap {
        return -1;
    }
    std::ptr::copy_nonoverlapping(blob.as_ptr(), out, blob.len());
    blob.len() as i64
}

#[no_mangle]
pub unsafe extern "C" fn video_decode(input: *const u8, len: i64, out: *mut u8, cap: i64) -> i64 {
    let (frames, _t, _h, _w) = decode(std::slice::from_raw_parts(input, len as usize));
    if frames.len() as i64 > cap {
        return -1;
    }
    std::ptr::copy_nonoverlapping(frames.as_ptr(), out, frames.len());
    frames.len() as i64
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn roundtrip() {
        // synthetic panning gradient: a moving texture so inter/skip/intra all fire
        let (t, h, w) = (5usize, 48usize, 64usize);
        let mut frames = vec![0u8; t * h * w];
        for f in 0..t {
            for y in 0..h {
                for x in 0..w {
                    let v = ((x + f * 2) ^ (y + f)) as u8;
                    frames[f * h * w + y * w + x] = v;
                }
            }
        }
        let blob = encode(&frames, t, h, w);
        let (dec, dt, dh, dw) = decode(&blob);
        assert_eq!((dt, dh, dw), (t, h, w));
        assert_eq!(dec, frames);
    }
}
