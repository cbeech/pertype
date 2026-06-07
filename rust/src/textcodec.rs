//! Trained per-file-type text/byte codec — byte-identical to `compressor/codec.py` +
//! `model.py`/`tokenizer.py`/`dictionary.py`/`freqmodel.py`/`arithmetic.py`. Loads a
//! Python-trained model (`CMP7`) and compresses/decompresses a file to the `CZ` container:
//!   transform → cost-optimal LZ + dictionary parse → arithmetic-coded token stream.
//! The decode path is pure integer arithmetic (always byte-exact); the cost-optimal parse
//! uses `f64` log2 prices exactly as the reference, so the encode is byte-identical too.

use std::collections::HashMap;

use crate::transform;

// --- bit I/O (MSB-first) -----------------------------------------------------

struct BitWriter {
    bytes: Vec<u8>,
    cur: u32,
    nbits: u32,
}
impl BitWriter {
    fn new() -> Self {
        BitWriter { bytes: Vec::new(), cur: 0, nbits: 0 }
    }
    fn write_bits(&mut self, value: u64, n: u32) {
        for shift in (0..n).rev() {
            self.cur = (self.cur << 1) | ((value >> shift) & 1) as u32;
            self.nbits += 1;
            if self.nbits == 8 {
                self.bytes.push(self.cur as u8);
                self.cur = 0;
                self.nbits = 0;
            }
        }
    }
    fn into_bytes(mut self) -> Vec<u8> {
        if self.nbits != 0 {
            self.bytes.push((self.cur << (8 - self.nbits)) as u8);
        }
        self.bytes
    }
}

struct BitReader<'a> {
    data: &'a [u8],
    pos: usize,
}
impl<'a> BitReader<'a> {
    fn new(data: &'a [u8]) -> Self {
        BitReader { data, pos: 0 }
    }
    fn read_bit(&mut self) -> u64 {
        let bi = self.pos >> 3;
        let bit = if bi >= self.data.len() {
            0 // arithmetic streams read zero-padding past the end
        } else {
            ((self.data[bi] >> (7 - (self.pos & 7))) & 1) as u64
        };
        self.pos += 1;
        bit
    }
}

// --- arithmetic coder (Witten–Neal–Cleary, 32-bit) --------------------------

const CODE_BITS: u32 = 32;
const MAXV: u64 = (1u64 << CODE_BITS) - 1;
const HALF: u64 = 1u64 << (CODE_BITS - 1);
const QUARTER: u64 = 1u64 << (CODE_BITS - 2);
const THREE_QUARTER: u64 = 3 * QUARTER;

struct AEnc {
    w: BitWriter,
    low: u64,
    high: u64,
    pending: u64,
}
impl AEnc {
    fn new() -> Self {
        AEnc { w: BitWriter::new(), low: 0, high: MAXV, pending: 0 }
    }
    fn emit(&mut self, bit: u32) {
        self.w.write_bits(bit as u64, 1);
        while self.pending > 0 {
            self.w.write_bits((bit ^ 1) as u64, 1);
            self.pending -= 1;
        }
    }
    fn encode(&mut self, cum: u64, freq: u64, total: u64) {
        let span = self.high - self.low + 1;
        self.high = self.low + span * (cum + freq) / total - 1;
        self.low += span * cum / total;
        loop {
            if self.high < HALF {
                self.emit(0);
            } else if self.low >= HALF {
                self.emit(1);
                self.low -= HALF;
                self.high -= HALF;
            } else if self.low >= QUARTER && self.high < THREE_QUARTER {
                self.pending += 1;
                self.low -= QUARTER;
                self.high -= QUARTER;
            } else {
                break;
            }
            self.low <<= 1;
            self.high = (self.high << 1) | 1;
        }
    }
    fn encode_bits(&mut self, value: u64, nbits: u32) {
        for shift in (0..nbits).rev() {
            self.encode((value >> shift) & 1, 1, 2);
        }
    }
    fn finish(mut self) -> Vec<u8> {
        self.pending += 1;
        self.emit(if self.low < QUARTER { 0 } else { 1 });
        self.w.into_bytes()
    }
}

struct ADec<'a> {
    r: BitReader<'a>,
    low: u64,
    high: u64,
    code: u64,
}
impl<'a> ADec<'a> {
    fn new(data: &'a [u8]) -> Self {
        let mut r = BitReader::new(data);
        let mut code = 0u64;
        for _ in 0..CODE_BITS {
            code = (code << 1) | r.read_bit();
        }
        ADec { r, low: 0, high: MAXV, code }
    }
    fn decode_target(&self, total: u64) -> u64 {
        let span = self.high - self.low + 1;
        ((self.code - self.low + 1) * total - 1) / span
    }
    fn update(&mut self, cum: u64, freq: u64, total: u64) {
        let span = self.high - self.low + 1;
        self.high = self.low + span * (cum + freq) / total - 1;
        self.low += span * cum / total;
        loop {
            if self.high < HALF {
            } else if self.low >= HALF {
                self.low -= HALF;
                self.high -= HALF;
                self.code -= HALF;
            } else if self.low >= QUARTER && self.high < THREE_QUARTER {
                self.low -= QUARTER;
                self.high -= QUARTER;
                self.code -= QUARTER;
            } else {
                break;
            }
            self.low <<= 1;
            self.high = (self.high << 1) | 1;
            self.code = (self.code << 1) | self.r.read_bit();
        }
    }
    fn decode_bits(&mut self, nbits: u32) -> u64 {
        let mut value = 0u64;
        for _ in 0..nbits {
            let bit = if self.decode_target(2) >= 1 { 1 } else { 0 };
            self.update(bit, 1, 2);
            value = (value << 1) | bit;
        }
        value
    }
}

// --- frequency model ---------------------------------------------------------

struct FreqModel {
    symbols: Vec<i64>,
    freqs: Vec<u64>,
    cum: Vec<u64>,
    total: u64,
    index: HashMap<i64, usize>,
}
impl FreqModel {
    fn deserialize(blob: &[u8]) -> Self {
        let n = u32::from_be_bytes([blob[0], blob[1], blob[2], blob[3]]) as usize;
        let mut pos = 4;
        let mut symbols = Vec::with_capacity(n);
        let mut freqs = Vec::with_capacity(n);
        for _ in 0..n {
            let s = u32::from_be_bytes([blob[pos], blob[pos + 1], blob[pos + 2], blob[pos + 3]]);
            let f = u32::from_be_bytes([blob[pos + 4], blob[pos + 5], blob[pos + 6], blob[pos + 7]]);
            symbols.push(s as i64);
            freqs.push(f as u64);
            pos += 8;
        }
        let mut cum = vec![0u64; n + 1];
        for i in 0..n {
            cum[i + 1] = cum[i] + freqs[i];
        }
        let total = cum[n];
        let index = symbols.iter().enumerate().map(|(i, &s)| (s, i)).collect();
        FreqModel { symbols, freqs, cum, total, index }
    }
    fn encode(&self, enc: &mut AEnc, sym: i64) {
        let i = self.index[&sym];
        enc.encode(self.cum[i], self.freqs[i], self.total);
    }
    fn decode(&self, dec: &mut ADec) -> i64 {
        let target = dec.decode_target(self.total);
        // bisect_right(cum, target) - 1
        let i = self.cum.partition_point(|&c| c <= target) - 1;
        dec.update(self.cum[i], self.freqs[i], self.total);
        self.symbols[i]
    }
    fn cost_bits(&self, sym: i64) -> f64 {
        let i = self.index[&sym];
        (self.total as f64 / self.freqs[i] as f64).log2()
    }
}

// --- dictionary --------------------------------------------------------------

struct Dictionary {
    patterns: Vec<Vec<u8>>,
    index: HashMap<[u8; 2], Vec<usize>>, // 2-byte prefix -> pattern ids, longest-first
}
impl Dictionary {
    fn deserialize(blob: &[u8]) -> Self {
        let count = u32::from_be_bytes([blob[0], blob[1], blob[2], blob[3]]) as usize;
        let mut pos = 4;
        let mut patterns = Vec::with_capacity(count);
        for _ in 0..count {
            let len = u16::from_be_bytes([blob[pos], blob[pos + 1]]) as usize;
            pos += 2;
            patterns.push(blob[pos..pos + len].to_vec());
            pos += len;
        }
        let mut index: HashMap<[u8; 2], Vec<usize>> = HashMap::new();
        for (pid, p) in patterns.iter().enumerate() {
            if p.len() >= 2 {
                index.entry([p[0], p[1]]).or_default().push(pid);
            }
        }
        for bucket in index.values_mut() {
            bucket.sort_by(|&a, &b| patterns[b].len().cmp(&patterns[a].len()));
        }
        Dictionary { patterns, index }
    }
    /// Longest pattern that is a prefix of `data[pos..]` → (pid, length).
    fn matcher(&self, data: &[u8], pos: usize, min_match: usize) -> Option<(usize, usize)> {
        if pos + 2 > data.len() {
            return None;
        }
        let bucket = self.index.get(&[data[pos], data[pos + 1]])?;
        for &pid in bucket {
            let pat = &self.patterns[pid];
            let len = pat.len();
            if len < min_match {
                continue;
            }
            if pos + len <= data.len() && &data[pos..pos + len] == pat.as_slice() {
                return Some((pid, len));
            }
        }
        None
    }
}

// --- value slots -------------------------------------------------------------

#[inline]
fn value_slot(v: u64) -> (u32, u64) {
    let slot = 63 - v.leading_zeros(); // bit_length(v) - 1, v >= 1
    (slot, v - (1u64 << slot))
}
#[inline]
fn value_from(slot: u32, extra: u64) -> u64 {
    (1u64 << slot) + extra
}

const MIN_MATCH: usize = 3;
const MAX_MATCH: usize = 1 << 12;
const WINDOW: usize = 1 << 19;
const MAX_CHAIN: usize = 128;
const MODE_NORMAL: i64 = 0;
const REP_N: usize = 16;

fn rep_init() -> Vec<i64> {
    (1..=REP_N as i64).collect()
}

// --- model -------------------------------------------------------------------

struct Model {
    type_id: String,
    version: u16,
    use_lz: bool,
    dictionary: Dictionary,
    main: FreqModel,
    dist: FreqModel,
    mode: FreqModel,
    transform: Vec<(u8, u8)>,
    blob: Vec<u8>,
}
impl Model {
    fn load(blob: &[u8]) -> Model {
        assert_eq!(&blob[..4], b"CMP7", "not a compressor model file");
        let version = u16::from_be_bytes([blob[4], blob[5]]);
        let use_lz = blob[6] != 0;
        let tid_len = blob[7] as usize;
        let type_id = String::from_utf8(blob[8..8 + tid_len].to_vec()).unwrap();
        let mut pos = 8 + tid_len;
        let mut chunks: Vec<&[u8]> = Vec::with_capacity(6);
        for _ in 0..6 {
            let n = u32::from_be_bytes([blob[pos], blob[pos + 1], blob[pos + 2], blob[pos + 3]])
                as usize;
            pos += 4;
            chunks.push(&blob[pos..pos + n]);
            pos += n;
        }
        let tspec_raw = chunks[4];
        let mut transform = Vec::new();
        let tn = tspec_raw[0] as usize;
        let mut tp = 1;
        for _ in 0..tn {
            transform.push((tspec_raw[tp], tspec_raw[tp + 1]));
            tp += 2;
        }
        Model {
            type_id,
            version,
            use_lz,
            dictionary: Dictionary::deserialize(chunks[0]),
            main: FreqModel::deserialize(chunks[1]),
            dist: FreqModel::deserialize(chunks[2]),
            mode: FreqModel::deserialize(chunks[3]),
            transform,
            blob: chunks[5].to_vec(),
        }
    }
    fn len_base(&self) -> i64 {
        256 + self.dictionary.patterns.len() as i64
    }
}

// --- tokens ------------------------------------------------------------------

#[derive(Clone)]
enum Tok {
    Lit(u8),
    Dict(usize),
    Match(usize, usize), // length, distance
}

// --- LZ forward match-finder (CSR over data positions) ----------------------

fn match_len(buf: &[u8], i: usize, j: usize, limit: usize) -> usize {
    let mut n = 0;
    while n < limit && buf[i + n] == buf[j + n] {
        n += 1;
    }
    n
}

struct Forward {
    off: Vec<usize>,
    cand_len: Vec<usize>,
    cand_dist: Vec<usize>,
}

fn lz_forward(combined: &[u8], base: usize) -> Forward {
    let nn = combined.len();
    let mut head: HashMap<[u8; 3], usize> = HashMap::new();
    let mut prev = vec![usize::MAX; nn];
    let insert = |head: &mut HashMap<[u8; 3], usize>, prev: &mut Vec<usize>, i: usize| {
        if i + MIN_MATCH <= nn {
            let key = [combined[i], combined[i + 1], combined[i + 2]];
            prev[i] = *head.get(&key).unwrap_or(&usize::MAX);
            head.insert(key, i);
        }
    };
    for i in 0..base {
        insert(&mut head, &mut prev, i);
    }
    let mut off = vec![0usize; nn - base + 1];
    let mut cand_len = Vec::new();
    let mut cand_dist = Vec::new();
    // ordered map length -> (vec index, dist)
    for p in base..nn {
        off[p - base] = cand_len.len();
        let mut found_idx: HashMap<usize, usize> = HashMap::new();
        let mut order: Vec<(usize, usize)> = Vec::new(); // (length, dist) insertion order
        if p + MIN_MATCH <= nn {
            let key = [combined[p], combined[p + 1], combined[p + 2]];
            let mut cand = *head.get(&key).unwrap_or(&usize::MAX);
            let mut chain = MAX_CHAIN;
            let limit = MAX_MATCH.min(nn - p);
            while cand != usize::MAX && p - cand <= WINDOW && chain > 0 {
                let length = match_len(combined, cand, p, limit);
                if length >= MIN_MATCH {
                    let dist = p - cand;
                    match found_idx.get(&length) {
                        Some(&oi) => {
                            if dist < order[oi].1 {
                                order[oi].1 = dist;
                            }
                        }
                        None => {
                            found_idx.insert(length, order.len());
                            order.push((length, dist));
                        }
                    }
                }
                cand = prev[cand];
                chain -= 1;
            }
        }
        for (length, dist) in &order {
            cand_len.push(*length);
            cand_dist.push(*dist);
        }
        insert(&mut head, &mut prev, p);
    }
    off[nn - base] = cand_len.len();
    Forward { off, cand_len, cand_dist }
}

// per-position longest dict match over combined[base..]
fn dict_matches(dict: &Dictionary, combined: &[u8], base: usize) -> (Vec<i64>, Vec<usize>) {
    let nn = combined.len();
    let mut dpid = vec![-1i64; nn - base];
    let mut dlen = vec![0usize; nn - base];
    for p in base..nn {
        if let Some((pid, len)) = dict.matcher(combined, p, MIN_MATCH) {
            dpid[p - base] = pid as i64;
            dlen[p - base] = len;
        }
    }
    (dpid, dlen)
}

// --- cost callables ----------------------------------------------------------

fn lit_cost(m: &Model, byte: u8) -> f64 {
    m.main.cost_bits(byte as i64)
}
fn dict_cost(m: &Model, pid: usize) -> f64 {
    m.main.cost_bits(256 + pid as i64)
}
fn match_cost(m: &Model, length: usize, distance: usize) -> f64 {
    let (lslot, _) = value_slot((length - MIN_MATCH + 1) as u64);
    let (dslot, _) = value_slot(distance as u64);
    let normal_mode = m.mode.cost_bits(MODE_NORMAL);
    m.main.cost_bits(m.len_base() + lslot as i64) + lslot as f64
        + normal_mode
        + m.dist.cost_bits(dslot as i64) + dslot as f64
}

// --- parsers -----------------------------------------------------------------

fn tokenize_optimal(m: &Model, data: &[u8]) -> Vec<Tok> {
    let base = m.blob.len();
    let combined: Vec<u8> = if base > 0 {
        let mut c = m.blob.clone();
        c.extend_from_slice(data);
        c
    } else {
        data.to_vec()
    };
    let nn = combined.len();
    let fwd = lz_forward(&combined, base);
    let (dpid, dlen) = dict_matches(&m.dictionary, &combined, base);

    let mut cost_to_end = vec![0f64; nn + 1];
    let mut choice: Vec<Tok> = vec![Tok::Lit(0); nn + 1];
    let mut p = nn;
    while p > base {
        p -= 1;
        let mut best = lit_cost(m, combined[p]) + cost_to_end[p + 1];
        let mut best_choice = Tok::Lit(combined[p]);
        let pi = p - base;
        if dlen[pi] >= MIN_MATCH {
            let c = dict_cost(m, dpid[pi] as usize) + cost_to_end[p + dlen[pi]];
            if c < best {
                best = c;
                best_choice = Tok::Dict(dpid[pi] as usize);
            }
        }
        for idx in fwd.off[pi]..fwd.off[pi + 1] {
            let (length, dist) = (fwd.cand_len[idx], fwd.cand_dist[idx]);
            let c = match_cost(m, length, dist) + cost_to_end[p + length];
            if c < best {
                best = c;
                best_choice = Tok::Match(length, dist);
            }
        }
        cost_to_end[p] = best;
        choice[p] = best_choice;
    }
    let mut tokens = Vec::new();
    let mut p = base;
    while p < nn {
        match choice[p].clone() {
            Tok::Lit(b) => {
                tokens.push(Tok::Lit(b));
                p += 1;
            }
            Tok::Dict(pid) => {
                tokens.push(Tok::Dict(pid));
                p += m.dictionary.patterns[pid].len();
            }
            Tok::Match(length, dist) => {
                tokens.push(Tok::Match(length, dist));
                p += length;
            }
        }
    }
    tokens
}

fn tokenize_dict_only(m: &Model, data: &[u8]) -> Vec<Tok> {
    let n = data.len();
    let mut tokens = Vec::new();
    let mut pos = 0;
    while pos < n {
        if let Some((pid, len)) = m.dictionary.matcher(data, pos, MIN_MATCH) {
            tokens.push(Tok::Dict(pid));
            pos += len;
        } else {
            tokens.push(Tok::Lit(data[pos]));
            pos += 1;
        }
    }
    tokens
}

fn detokenize(m: &Model, tokens: &[Tok]) -> Vec<u8> {
    let base = m.blob.len();
    let mut out = m.blob.clone();
    for tok in tokens {
        match tok {
            Tok::Lit(b) => out.push(*b),
            Tok::Dict(pid) => out.extend_from_slice(&m.dictionary.patterns[*pid]),
            Tok::Match(length, distance) => {
                let start = out.len() - distance;
                for k in 0..*length {
                    out.push(out[start + k]);
                }
            }
        }
    }
    out[base..].to_vec()
}

// --- token stream entropy coding --------------------------------------------

fn encode_tokens(m: &Model, tokens: &[Tok]) -> Vec<u8> {
    let mut enc = AEnc::new();
    let len_base = m.len_base();
    let mut reps = rep_init();
    for tok in tokens {
        match tok {
            Tok::Lit(b) => m.main.encode(&mut enc, *b as i64),
            Tok::Dict(pid) => m.main.encode(&mut enc, 256 + *pid as i64),
            Tok::Match(length, distance) => {
                let (lslot, lextra) = value_slot((length - MIN_MATCH + 1) as u64);
                m.main.encode(&mut enc, len_base + lslot as i64);
                enc.encode_bits(lextra, lslot);
                let d = *distance as i64;
                if let Some(i) = reps.iter().position(|&r| r == d) {
                    m.mode.encode(&mut enc, (i + 1) as i64);
                    reps.remove(i);
                } else {
                    m.mode.encode(&mut enc, MODE_NORMAL);
                    let (dslot, dextra) = value_slot(*distance as u64);
                    m.dist.encode(&mut enc, dslot as i64);
                    enc.encode_bits(dextra, dslot);
                    reps.pop();
                }
                reps.insert(0, d);
            }
        }
    }
    enc.finish()
}

fn decode_tokens(m: &Model, payload: &[u8], n_tokens: usize) -> Vec<Tok> {
    let mut dec = ADec::new(payload);
    let len_base = m.len_base();
    let n_patterns = m.dictionary.patterns.len() as i64;
    let mut reps = rep_init();
    let mut tokens = Vec::with_capacity(n_tokens);
    for _ in 0..n_tokens {
        let sym = m.main.decode(&mut dec);
        if sym < 256 {
            tokens.push(Tok::Lit(sym as u8));
        } else if sym < 256 + n_patterns {
            tokens.push(Tok::Dict((sym - 256) as usize));
        } else {
            let lslot = (sym - len_base) as u32;
            let length =
                value_from(lslot, dec.decode_bits(lslot)) as usize + MIN_MATCH - 1;
            let mm = m.mode.decode(&mut dec);
            let distance: i64;
            if mm == MODE_NORMAL {
                let dslot = m.dist.decode(&mut dec) as u32;
                distance = value_from(dslot, dec.decode_bits(dslot)) as i64;
                reps.pop();
            } else {
                distance = reps[(mm - 1) as usize];
                reps.remove((mm - 1) as usize);
            }
            reps.insert(0, distance);
            tokens.push(Tok::Match(length, distance as usize));
        }
    }
    tokens
}

// --- CZ container ------------------------------------------------------------

const MAGIC: u8 = 0xC7;
const FMT_VERSION: u8 = 5;

fn crc32(data: &[u8]) -> u32 {
    let mut c = flate2::Crc::new();
    c.update(data);
    c.sum()
}

fn id_hash(type_id: &str, version: u16) -> u16 {
    (crc32(format!("{}:{}", type_id, version).as_bytes()) & 0xFFFF) as u16
}

fn write_varint(buf: &mut Vec<u8>, mut n: u64) {
    loop {
        let b = (n & 0x7F) as u8;
        n >>= 7;
        if n != 0 {
            buf.push(b | 0x80);
        } else {
            buf.push(b);
            return;
        }
    }
}

fn read_varint(blob: &[u8], pos: &mut usize) -> u64 {
    let mut val = 0u64;
    let mut shift = 0u32;
    loop {
        let b = blob[*pos];
        *pos += 1;
        val |= ((b & 0x7F) as u64) << shift;
        if b & 0x80 == 0 {
            return val;
        }
        shift += 7;
    }
}

pub fn compress(model_blob: &[u8], data: &[u8]) -> Vec<u8> {
    let m = Model::load(model_blob);
    let tdata = transform::apply(data, &m.transform);
    let tokens = if m.use_lz {
        tokenize_optimal(&m, &tdata)
    } else {
        tokenize_dict_only(&m, &tdata)
    };
    let payload = encode_tokens(&m, &tokens);

    let mut out = Vec::new();
    out.push(MAGIC);
    out.push(FMT_VERSION);
    out.extend_from_slice(&id_hash(&m.type_id, m.version).to_be_bytes());
    write_varint(&mut out, data.len() as u64);
    write_varint(&mut out, tokens.len() as u64);
    out.extend_from_slice(&crc32(data).to_be_bytes());
    out.extend_from_slice(&payload);
    out
}

pub fn decompress(model_blob: &[u8], blob: &[u8]) -> Vec<u8> {
    let m = Model::load(model_blob);
    assert!(!blob.is_empty() && blob[0] == MAGIC, "not a CZ container");
    assert_eq!(blob[1], FMT_VERSION, "unsupported container format version");
    let idh = u16::from_be_bytes([blob[2], blob[3]]);
    assert_eq!(idh, id_hash(&m.type_id, m.version), "model mismatch");
    let mut pos = 4;
    let orig_len = read_varint(blob, &mut pos) as usize;
    let n_tokens = read_varint(blob, &mut pos) as usize;
    let crc = u32::from_be_bytes([blob[pos], blob[pos + 1], blob[pos + 2], blob[pos + 3]]);
    pos += 4;

    let tokens = decode_tokens(&m, &blob[pos..], n_tokens);
    let tdata = detokenize(&m, &tokens);
    let data = transform::invert(&tdata, &m.transform);

    assert_eq!(data.len(), orig_len, "length mismatch");
    assert_eq!(crc32(&data), crc, "checksum mismatch");
    data
}

// --- C ABI ------------------------------------------------------------------

#[no_mangle]
pub unsafe extern "C" fn text_compress(
    model: *const u8, model_len: i64, data: *const u8, data_len: i64, out: *mut u8, cap: i64,
) -> i64 {
    let m = std::slice::from_raw_parts(model, model_len as usize);
    let d = std::slice::from_raw_parts(data, data_len as usize);
    let blob = compress(m, d);
    if blob.len() as i64 > cap {
        return -1;
    }
    std::ptr::copy_nonoverlapping(blob.as_ptr(), out, blob.len());
    blob.len() as i64
}

#[no_mangle]
pub unsafe extern "C" fn text_decompress(
    model: *const u8, model_len: i64, data: *const u8, data_len: i64, out: *mut u8, cap: i64,
) -> i64 {
    let m = std::slice::from_raw_parts(model, model_len as usize);
    let d = std::slice::from_raw_parts(data, data_len as usize);
    let blob = decompress(m, d);
    if blob.len() as i64 > cap {
        return -1;
    }
    std::ptr::copy_nonoverlapping(blob.as_ptr(), out, blob.len());
    blob.len() as i64
}
