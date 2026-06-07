//! MED (JPEG-LS) predictor — byte-identical to `compressor/predictors.py`. Forward gives the
//! residual `P - pred`; reconstruct replays it causally. (GAP/CALIC live in `calic`.)

const ORIGIN: i64 = 128;

#[inline]
fn med_pred(p: &[i64], w: usize, y: usize, x: usize) -> i64 {
    let i = y * w + x;
    if y == 0 && x == 0 {
        ORIGIN
    } else if y == 0 {
        p[i - 1] // first row: predict from the left
    } else if x == 0 {
        p[i - w] // first col: predict from above
    } else {
        let (a, b, c) = (p[i - 1], p[i - w], p[i - w - 1]);
        let (mx, mn) = if a > b { (a, b) } else { (b, a) };
        if c >= mx {
            mn
        } else if c <= mn {
            mx
        } else {
            a + b - c
        }
    }
}

pub fn med_residual(p: &[i64], h: usize, w: usize) -> Vec<i64> {
    let mut res = vec![0i64; h * w];
    for y in 0..h {
        for x in 0..w {
            let i = y * w + x;
            res[i] = p[i] - med_pred(p, w, y, x);
        }
    }
    res
}

pub fn med_reconstruct(res: &[i64], h: usize, w: usize) -> Vec<i64> {
    let mut p = vec![0i64; h * w];
    for y in 0..h {
        for x in 0..w {
            let i = y * w + x;
            p[i] = med_pred(&p, w, y, x) + res[i];
        }
    }
    p
}
