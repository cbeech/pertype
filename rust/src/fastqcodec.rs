//! FQS1 whole-file FASTQ codec — byte-identical to `pertype/fastqcodec.py`
//! and the C twin (`pertype/_native/fastq.c`). Template+delta headers,
//! reverse-complement-oriented 2-bit sequences under raw LZMA2 (liblzma
//! preset 9, via lzma-sys FFI), and a caller-supplied qualcodec quality
//! payload. The small streams use an adaptive order-1 byte-context model
//! on the shared WNC coder (INCR 32, RESCALE 1<<14, (x+1)>>1 halving).

use crate::arith::*;

pub const KMER: usize = 12;
const KMASK: u64 = (1 << (2 * KMER)) - 1;

fn wv(buf: &mut Vec<u8>, mut n: u64) {
    while n >= 0x80 {
        buf.push((n as u8) | 0x80);
        n >>= 7;
    }
    buf.push(n as u8);
}

struct Rdr<'a> {
    b: &'a [u8],
    pos: usize,
}

impl<'a> Rdr<'a> {
    fn new(b: &'a [u8]) -> Self {
        Rdr { b, pos: 0 }
    }
    fn rv(&mut self) -> Option<u64> {
        let mut n: u64 = 0;
        let mut shift = 0;
        loop {
            if self.pos >= self.b.len() || shift > 63 {
                return None;
            }
            let x = self.b[self.pos];
            self.pos += 1;
            n |= ((x & 0x7F) as u64) << shift;
            if x & 0x80 == 0 {
                return Some(n);
            }
            shift += 7;
        }
    }
}

fn b2i(c: u8) -> i32 {
    match c {
        b'A' => 0,
        b'C' => 1,
        b'G' => 2,
        b'T' => 3,
        _ => -1,
    }
}

// ------------------------------------------------------------- ctxblob ----
struct Ctx {
    cnt: [i32; 256],
    tot: i64,
}

fn ctx_encode(data: &[u8]) -> Vec<u8> {
    let mut ctx: Vec<Ctx> = (0..256)
        .map(|_| Ctx { cnt: [1; 256], tot: 256 })
        .collect();
    let mut out = Vec::new();
    wv(&mut out, data.len() as u64);
    let mut e = Enc::new();
    let mut prev = 0usize;
    for &b in data {
        let c = &mut ctx[prev];
        let mut cum = 0u64;
        for s in 0..b as usize {
            cum += c.cnt[s] as u64;
        }
        e.encode(cum, c.cnt[b as usize] as u64, c.tot as u64);
        c.cnt[b as usize] += INCR as i32;
        c.tot += INCR;
        if c.tot >= RESCALE {
            let mut t = 0i64;
            for s in 0..256 {
                c.cnt[s] = (c.cnt[s] + 1) >> 1;
                t += c.cnt[s] as i64;
            }
            c.tot = t;
        }
        prev = b as usize;
    }
    out.extend(e.finish());
    out
}

fn ctx_decode(blob: &[u8]) -> Option<Vec<u8>> {
    let mut r = Rdr::new(blob);
    let n = r.rv()? as usize;
    let mut ctx: Vec<Ctx> = (0..256)
        .map(|_| Ctx { cnt: [1; 256], tot: 256 })
        .collect();
    let mut d = Dec::new(&blob[r.pos..]);
    let mut out = Vec::with_capacity(n);
    let mut prev = 0usize;
    for _ in 0..n {
        let c = &mut ctx[prev];
        let total = c.tot as u64;
        let target = d.target(total);
        let mut cum = 0u64;
        let mut s = 0usize;
        while s < 255 && cum + c.cnt[s] as u64 <= target {
            cum += c.cnt[s] as u64;
            s += 1;
        }
        d.update(cum, c.cnt[s] as u64, total);
        out.push(s as u8);
        c.cnt[s] += INCR as i32;
        c.tot += INCR;
        if c.tot >= RESCALE {
            let mut t = 0i64;
            for j in 0..256 {
                c.cnt[j] = (c.cnt[j] + 1) >> 1;
                t += c.cnt[j] as i64;
            }
            c.tot = t;
        }
        prev = s;
    }
    Some(out)
}

// ------------------------------------------------------------- lzma FFI ---
mod lzma {
    use lzma_sys::*;
    use std::ptr;

    pub fn raw_encode(data: &[u8]) -> Option<Vec<u8>> {
        unsafe {
            let mut opts: lzma_options_lzma = std::mem::zeroed();
            // lzma_lzma_preset returns false (0) on success, true on error.
            if lzma_lzma_preset(&mut opts, 9) != 0 {
                return None;
            }
            let filters = [
                lzma_filter { id: LZMA_FILTER_LZMA2, options: &mut opts as *mut _ as *mut std::ffi::c_void },
                lzma_filter { id: LZMA_VLI_UNKNOWN, options: ptr::null_mut() },
            ];
            let mut out = vec![0u8; data.len() + data.len() / 2 + 65536];
            let mut out_pos = 0usize;
            let rc = lzma_raw_buffer_encode(
                filters.as_ptr(), ptr::null(), data.as_ptr(), data.len(),
                out.as_mut_ptr(), &mut out_pos, out.len(),
            );
            if rc != LZMA_OK {
                return None;
            }
            out.truncate(out_pos);
            Some(out)
        }
    }

    pub fn raw_decode(data: &[u8], outlen: usize) -> Option<Vec<u8>> {
        unsafe {
            let mut opts: lzma_options_lzma = std::mem::zeroed();
            if lzma_lzma_preset(&mut opts, 9) != 0 {
                return None;
            }
            let filters = [
                lzma_filter { id: LZMA_FILTER_LZMA2, options: &mut opts as *mut _ as *mut std::ffi::c_void },
                lzma_filter { id: LZMA_VLI_UNKNOWN, options: ptr::null_mut() },
            ];
            let mut out = vec![0u8; outlen];
            let mut in_pos = 0usize;
            let mut out_pos = 0usize;
            let rc = lzma_raw_buffer_decode(
                filters.as_ptr(), ptr::null(), data.as_ptr(), &mut in_pos, data.len(),
                out.as_mut_ptr(), &mut out_pos, out.len(),
            );
            if rc != LZMA_OK || out_pos != outlen {
                return None;
            }
            Some(out)
        }
    }
}

// --------------------------------------------------------- header model ---
const MAX_RUNS: usize = 64;

#[derive(Default, Clone)]
struct HParse<'a> {
    flags: Vec<u8>,          // 1 = integer run
    txts: Vec<&'a [u8]>,     // text runs verbatim
    ivals: Vec<i64>,         // integer values
    iw: Vec<usize>,          // integer run raw widths
}

fn parse_header(h: &[u8]) -> Option<HParse<'_>> {
    let mut p = HParse::default();
    let n = h.len();
    let mut i = 0;
    while i < n {
        let mut j = i;
        if h[i].is_ascii_digit() {
            while j < n && h[j].is_ascii_digit() {
                j += 1;
            }
            if j - i > 18 {
                return None; // int64 safety margin; Python handles, C/Rust decline
            }
            let mut v: i64 = 0;
            for &c in &h[i..j] {
                v = v * 10 + (c - b'0') as i64;
            }
            p.ivals.push(v);
            p.iw.push(j - i);
            p.flags.push(1);
        } else {
            while j < n && !h[j].is_ascii_digit() {
                j += 1;
            }
            p.txts.push(&h[i..j]);
            p.flags.push(0);
        }
        i = j;
        if p.flags.len() > MAX_RUNS {
            return None;
        }
    }
    Some(p)
}

fn take<'a>(blob: &'a [u8], pos: &mut usize) -> Option<&'a [u8]> {
    let mut r = Rdr { b: blob, pos: *pos };
    let l = r.rv()? as usize;
    *pos = r.pos;
    if *pos + l > blob.len() {
        return None;
    }
    let s = &blob[*pos..*pos + l];
    *pos += l;
    Some(s)
}

fn padint(v: i64, width: usize) -> Vec<u8> {
    let t = v.to_string().into_bytes();
    if t.len() >= width {
        t
    } else {
        let mut out = vec![b'0'; width - t.len()];
        out.extend(t);
        out
    }
}

// ------------------------------------------------------------- encode -----
pub fn encode(fastq: &[u8], qpayload: &[u8]) -> Option<Vec<u8>> {
    if fastq.is_empty() {
        return None;
    }
    // split lines (final empty piece after a trailing newline is not kept)
    let mut lines: Vec<&[u8]> = Vec::new();
    let mut start = 0usize;
    for (i, &c) in fastq.iter().enumerate() {
        if c == b'\n' {
            lines.push(&fastq[start..i]);
            start = i + 1;
        }
    }
    let trailing = start == fastq.len();
    if !trailing {
        lines.push(&fastq[start..]);
    }
    if lines.is_empty() || lines.len() % 4 != 0 {
        return { eprintln!("encode None at step {}", "0"); None }
    }
    let nrec = lines.len() / 4;
    let mut lengths = Vec::with_capacity(nrec);
    for r in 0..nrec {
        let (sq, ql) = (lines[4 * r + 1], lines[4 * r + 3]);
        if sq.len() != ql.len() {
            return { eprintln!("encode None at step {}", "1"); None }
        }
        if ql.iter().any(|&b| b < 33 || b > 126) {
            return { eprintln!("encode None at step {}", "2"); None }
        }
        lengths.push(ql.len() as u64);
    }

    // header template
    let hp0 = parse_header(lines[0])?;
    let ncols = hp0.ivals.len();
    let mut tmpl = Vec::new();
    wv(&mut tmpl, hp0.flags.len() as u64);
    let (mut ti, mut ci) = (0usize, 0usize);
    for &f in &hp0.flags {
        tmpl.push(f);
        if f == 0 {
            wv(&mut tmpl, hp0.txts[ti].len() as u64);
            tmpl.extend(hp0.txts[ti]);
            ti += 1;
        } else {
            wv(&mut tmpl, hp0.iw[ci] as u64);
            ci += 1;
        }
    }

    // per-column delta streams + exceptions
    let mut cols: Vec<Vec<u8>> = vec![Vec::new(); ncols];
    for c in 0..ncols {
        wv(&mut cols[c], hp0.ivals[c] as u64);
    }
    let mut prev = hp0.ivals.clone();
    let mut exc = Vec::new();
    let mut nexc = 0u64;
    let mut last_idx = 0usize;
    for idx in 1..nrec {
        let h = lines[4 * idx];
        let hp = parse_header(h);
        let mut bad = hp.is_none();
        if !bad {
            let hp = hp.as_ref().unwrap();
            if hp.flags != hp0.flags || hp.txts != hp0.txts {
                bad = true;
            } else {
                // each integer run must equal its zero-padded form
                let mut pos2 = 0usize;
                let mut c2 = 0usize;
                while pos2 < h.len() && !bad {
                    let mut j = pos2;
                    if h[pos2].is_ascii_digit() {
                        while j < h.len() && h[j].is_ascii_digit() {
                            j += 1;
                        }
                        if padint(hp.ivals[c2], hp0.iw[c2]) != h[pos2..j] {
                            bad = true;
                        }
                        c2 += 1;
                    } else {
                        while j < h.len() && !h[j].is_ascii_digit() {
                            j += 1;
                        }
                    }
                    pos2 = j;
                }
            }
        }
        if bad {
            nexc += 1;
            wv(&mut exc, (idx - last_idx) as u64);
            last_idx = idx;
            wv(&mut exc, h.len() as u64);
            exc.extend(h);
            continue;
        }
        let hp = hp.unwrap();
        for c in 0..ncols {
            wv(&mut cols[c], zigzag(hp.ivals[c] - prev[c]));
        }
        prev = hp.ivals;
    }

    // lengths
    let mut lraw = Vec::new();
    for &l in &lengths {
        wv(&mut lraw, l);
    }

    // plus lines
    let pflag = lines
        .iter()
        .skip(2)
        .step_by(4)
        .any(|p| *p != b"+");
    let mut praw = Vec::new();
    if pflag {
        for r in 0..nrec {
            wv(&mut praw, lines[4 * r + 2].len() as u64);
            praw.extend(lines[4 * r + 2]);
        }
    }

    // orientation + 2-bit pack
    let mut seen = vec![0u8; 1 << (2 * KMER)];
    let mut bmp = Vec::new();
    let mut cur = 0u8;
    let mut nbb = 0;
    let mut packed = Vec::new();
    let mut pcur = 0u8;
    let mut pnb = 0;
    let mut nbase: u64 = 0;
    let mut npraw = Vec::new();
    let mut nprev: u64 = 0;
    for r in 0..nrec {
        let s = lines[4 * r + 1];
        let mut kmer = 0u64;
        let mut run = 0usize;
        let mut hf = 0i64;
        let mut hr = 0i64;
        for &c in s {
            let v = b2i(c);
            if v < 0 {
                kmer = 0;
                run = 0;
                continue;
            }
            kmer = ((kmer << 2) | v as u64) & KMASK;
            run += 1;
            if run >= KMER {
                hf += seen[kmer as usize] as i64;
            }
        }
        kmer = 0;
        run = 0;
        for j in (0..s.len()).rev() {
            let v = b2i(s[j]);
            if v < 0 {
                kmer = 0;
                run = 0;
                continue;
            }
            kmer = ((kmer << 2) | (3 - v) as u64) & KMASK;
            run += 1;
            if run >= KMER {
                hr += seen[kmer as usize] as i64;
            }
        }
        let flip = hr > hf;
        cur = (cur << 1) | flip as u8;
        nbb += 1;
        if nbb == 8 {
            bmp.push(cur);
            cur = 0;
            nbb = 0;
        }
        kmer = 0;
        run = 0;
        for jj in 0..s.len() {
            let j = if flip { s.len() - 1 - jj } else { jj };
            let mut v = b2i(s[j]);
            if v >= 0 && flip {
                v = 3 - v;
            }
            if v < 0 {
                wv(&mut npraw, nbase - nprev);
                nprev = nbase;
                v = 0;
                kmer = 0;
                run = 0;
            } else {
                kmer = ((kmer << 2) | v as u64) & KMASK;
                run += 1;
            }
            pcur = (pcur << 2) | v as u8;
            nbase += 1;
            pnb += 1;
            if pnb == 4 {
                packed.push(pcur);
                pcur = 0;
                pnb = 0;
            }
            if v >= 0 && run >= KMER {
                seen[kmer as usize] = 1;
            }
        }
    }
    if nbb > 0 {
        bmp.push(cur << (8 - nbb));
    }
    if pnb > 0 {
        packed.push(pcur << (2 * (4 - pnb)));
    }
    let lz = lzma::raw_encode(&packed)?;

    // assemble
    let mut out = Vec::new();
    out.extend(b"FQS1");
    out.extend((nrec as u64).to_be_bytes());
    out.push(trailing as u8);
    wv(&mut out, tmpl.len() as u64);
    out.extend(tmpl);
    for cb in &cols {
        let coded = ctx_encode(cb);
        wv(&mut out, coded.len() as u64);
        out.extend(coded);
    }
    for raw in [&lraw, &bmp, &npraw] {
        let coded = ctx_encode(raw);
        wv(&mut out, coded.len() as u64);
        out.extend(coded);
    }
    wv(&mut out, nexc);
    out.extend(exc);
    out.push(pflag as u8);
    if pflag {
        let coded = ctx_encode(&praw);
        wv(&mut out, coded.len() as u64);
        out.extend(coded);
    }
    out.extend(nbase.to_be_bytes());
    wv(&mut out, lz.len() as u64);
    out.extend(lz);
    wv(&mut out, qpayload.len() as u64);
    out.extend(qpayload);
    Some(out)
}

// ------------------------------------------------------------- decode -----
pub fn decode(blob: &[u8], qflat: &[u8]) -> Option<Vec<u8>> {
    if blob.len() < 13 || &blob[..4] != b"FQS1" {
        return None;
    }
    let nrec = u64::from_be_bytes(blob[4..12].try_into().ok()?) as usize;
    let trailing = blob[12];
    let mut pos = 13usize;


    // template
    let tmpl = take(blob, &mut pos)?;
    let mut tp = Rdr::new(tmpl);
    let nruns = tp.rv()? as usize;
    if nruns > MAX_RUNS {
        return None;
    }
    let mut flags = Vec::with_capacity(nruns);
    let mut txts: Vec<&[u8]> = Vec::new();
    let mut widths: Vec<usize> = Vec::new();
    for _ in 0..nruns {
        if tp.pos >= tmpl.len() {
            return None;
        }
        let f = tmpl[tp.pos];
        tp.pos += 1;
        flags.push(f);
        if f == 0 {
            let l = tp.rv()? as usize;
            if tp.pos + l > tmpl.len() {
                return None;
            }
            txts.push(&tmpl[tp.pos..tp.pos + l]);
            tp.pos += l;
        } else {
            widths.push(tp.rv()? as usize);
        }
    }
    let ncols = widths.len();

    // per-column value streams
    let mut colvals: Vec<Vec<i64>> = Vec::with_capacity(ncols);
    for _ in 0..ncols {
        let raw = ctx_decode(take(blob, &mut pos)?)?;
        let mut r = Rdr::new(&raw);
        let first = r.rv()? as i64;
        let mut vals = vec![first];
        while r.pos < raw.len() {
            let m = r.rv()?;
            let v = vals[vals.len() - 1] + unzigzag(m);
            vals.push(v);
        }
        colvals.push(vals);
    }

    // lengths
    let lraw = ctx_decode(take(blob, &mut pos)?)?;
    let mut lengths = Vec::with_capacity(nrec);
    {
        let mut r = Rdr::new(&lraw);
        for _ in 0..nrec {
            lengths.push(r.rv()? as usize);
        }
    }

    // bitmap
    let bmp = ctx_decode(take(blob, &mut pos)?)?;

    // N positions
    let npraw = ctx_decode(take(blob, &mut pos)?)?;
    let mut npos = Vec::new();
    {
        let mut r = Rdr::new(&npraw);
        let mut prev = 0i64;
        while r.pos < npraw.len() {
            let d = r.rv()? as i64;
            prev += d;
            npos.push(prev);
        }
    }

    // exceptions
    let mut er = Rdr { b: blob, pos };
    let nexc = er.rv()? as usize;
    pos = er.pos;
    let mut exc_idx = Vec::with_capacity(nexc);
    let mut exc_h: Vec<&[u8]> = Vec::with_capacity(nexc);
    {
        let mut last = 0usize;
        for _ in 0..nexc {
            let d = er.rv()? as usize;
            last += d;
            let l = er.rv()? as usize;
            if er.pos + l > blob.len() {
                return None;
            }
            exc_idx.push(last);
            exc_h.push(&blob[er.pos..er.pos + l]);
            er.pos += l;
        }
        pos = er.pos;
    }

    // plus
    if pos >= blob.len() {
        return None;
    }
    let pflag = blob[pos];
    pos += 1;
    let mut plusp: Vec<&[u8]> = Vec::new();
    let mut praw: Vec<u8> = Vec::new();
    if pflag != 0 {
        praw = ctx_decode(take(blob, &mut pos)?)?;
        let mut r = Rdr::new(&praw);
        for _ in 0..nrec {
            let l = r.rv()? as usize;
            if r.pos + l > praw.len() {
                return None;
            }
            plusp.push(&praw[r.pos..r.pos + l]);
            r.pos += l;
        }
    }

    // packed sequences
    if pos + 8 > blob.len() {
        return None;
    }
    let nbase = u64::from_be_bytes(blob[pos..pos + 8].try_into().ok()?) as usize;
    pos += 8;
    let lzs = take(blob, &mut pos)?;
    let packed = lzma::raw_decode(lzs, (nbase + 3) / 4)?;
    let _qp = take(blob, &mut pos)?; // quality payload (decoded by the caller)
    if pos != blob.len() {
        return None;
    }

    // rebuild the original FASTQ bytes
    let mut out = Vec::with_capacity(qflat.len() * 2 + nrec * 32);
    let mut qi = 0usize;
    let mut npi = 0usize;
    let mut seqoff = 0usize;
    let mut e_i = 0usize;
    let mut seq_idx = 0usize;
    for i in 0..nrec {
        // header
        if e_i < exc_idx.len() && exc_idx[e_i] == i {
            out.extend(exc_h[e_i]);
            e_i += 1;
        } else {
            let mut ci = 0usize;
            let mut ti = 0usize;
            for &f in &flags {
                if f == 0 {
                    out.extend(txts[ti]);
                    ti += 1;
                } else {
                    out.extend(padint(colvals[ci][seq_idx], widths[ci]));
                    ci += 1;
                }
            }
            seq_idx += 1;
        }
        out.push(b'\n');
        // sequence
        let lr = lengths[i];
        let flip = (bmp[i / 8] >> (7 - (i % 8))) & 1;
        let seq_start = out.len();
        out.resize(seq_start + lr, 0);
        for j in 0..lr {
            let g = seqoff + j;
            let ch = if npi < npos.len() && npos[npi] == g as i64 {
                npi += 1;
                b'N'
            } else {
                b"ACGT"[((packed[g / 4] >> (6 - 2 * (g % 4))) & 3) as usize]
            };
            if flip == 1 {
                let cc = match ch {
                    b'A' => b'T',
                    b'C' => b'G',
                    b'G' => b'C',
                    b'T' => b'A',
                    other => other,
                };
                out[seq_start + (lr - 1 - j)] = cc;
            } else {
                out[seq_start + j] = ch;
            }
        }
        seqoff += lr;
        out.push(b'\n');
        // plus
        if pflag != 0 {
            out.extend(plusp[i]);
        } else {
            out.push(b'+');
        }
        out.push(b'\n');
        // quality
        if qi + lr > qflat.len() {
            return None;
        }
        out.extend(&qflat[qi..qi + lr]);
        qi += lr;
        if i < nrec - 1 || trailing != 0 {
            out.push(b'\n');
        }
    }
    Some(out)
}

// ------------------------------------------------------------- C ABI ------
#[no_mangle]
pub unsafe extern "C" fn fastq_encode(data: *const u8, dlen: i64,
                                      qp: *const u8, qplen: i64,
                                      out: *mut u8, cap: i64) -> i64 {
    let data = std::slice::from_raw_parts(data, dlen as usize);
    let qp = std::slice::from_raw_parts(qp, qplen as usize);
    match encode(data, qp) {
        Some(bytes) => {
            if bytes.len() as i64 > cap {
                return -1;
            }
            std::ptr::copy_nonoverlapping(bytes.as_ptr(), out, bytes.len());
            bytes.len() as i64
        }
        None => -1,
    }
}

#[no_mangle]
pub unsafe extern "C" fn fastq_decode(blob: *const u8, blen: i64,
                                      qflat: *const u8, qflen: i64,
                                      out: *mut u8, cap: i64) -> i64 {
    let blob = std::slice::from_raw_parts(blob, blen as usize);
    let qflat = std::slice::from_raw_parts(qflat, qflen as usize);
    match decode(blob, qflat) {
        Some(bytes) => {
            if bytes.len() as i64 > cap {
                return -1;
            }
            std::ptr::copy_nonoverlapping(bytes.as_ptr(), out, bytes.len());
            bytes.len() as i64
        }
        None => -1,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rt(data: &[u8]) {
        // quality payload is opaque to this codec; use a dummy payload and
        // pass quality bytes straight through as qflat for the round-trip.
        let lines: Vec<&[u8]> = data.split(|&c| c == b'\n').collect();
        let rec: &[&[u8]] = if lines.last().map(|l| l.is_empty()) == Some(true) { &lines[..lines.len() - 1] } else { &lines[..] };
        let quals: Vec<u8> = rec.iter().skip(3).step_by(4).copied().collect::<Vec<_>>().concat();
        let blob = encode(data, b"QP").unwrap();
        assert_eq!(&decode(&blob, &quals).unwrap(), &data.to_vec());
    }

    #[test]
    fn roundtrip_basic() {
        rt(b"@INST.1.1 1:N:0:AC\nACGTNACGTN\n+\n#8ACCGGGFF\n@INST.1.2 2:N:0:AC\nTTTTGGGGCC\n+\nFFFFFFFFFF\n");
    }

    #[test]
    fn roundtrip_leading_zeros_and_exception() {
        rt(b"@DRR063436.1 1/1\nACGTACGT\n+\nFFFFFFFF\n@DRR063436.2 2/1\nTTTTGGGG\n+\nGGGGGGGG\n@weird header!\nCCCCAAAA\n+\nHHHHHHHH\n");
    }
}

#[cfg(test)]
mod lzma_probe {
    #[test]
    fn probe_raw_encode() {
        use lzma_sys::*;
        unsafe {
            let mut opts: lzma_options_lzma = std::mem::zeroed();
            let prc = lzma_lzma_preset(&mut opts, 9);
            eprintln!("preset rc = {}", prc);
            let filters = [
                lzma_filter { id: LZMA_FILTER_LZMA2, options: &mut opts as *mut _ as *mut std::ffi::c_void },
                lzma_filter { id: LZMA_VLI_UNKNOWN, options: std::ptr::null_mut() },
            ];
            let data = b"ACGTACGTACGTACGTACGTACGTACGTACGT";
            let mut out = vec![0u8; 1024];
            let mut out_pos = 0usize;
            let rc = lzma_raw_buffer_encode(
                filters.as_ptr(), std::ptr::null(), data.as_ptr(), data.len(),
                out.as_mut_ptr(), &mut out_pos, out.len(),
            );
            eprintln!("encode rc = {} out_pos = {}", rc, out_pos);
            assert_eq!(rc, LZMA_OK);
        }
    }
}
