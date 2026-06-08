//! Trained per-file-type text/byte codec — byte-identical to `compressor/codec.py` +
//! `model.py`/`tokenizer.py`/`dictionary.py`/`freqmodel.py`/`arithmetic.py`. Loads a
//! Python-trained model (`CMP7`) and compresses/decompresses a file to the `CZ` container:
//!   transform → cost-optimal LZ + dictionary parse → arithmetic-coded token stream.
//! The decode path is pure integer arithmetic (always byte-exact); the cost-optimal parse
//! uses `f64` log2 prices exactly as the reference, so the encode is byte-identical too.

use std::collections::HashMap;
use std::hash::{BuildHasherDefault, Hasher};

use crate::transform;

/// Fast hasher for the integer keys used by the LZ/dict match-finders. The default
/// `SipHash` dominates the parse on small keys; a single multiply (Fibonacci hashing)
/// distributes 24-/16-bit keys well and is ~an order of magnitude cheaper. Keys stay
/// exact (the map still resolves true collisions), so the produced tokens are unchanged.
#[derive(Default)]
struct IntHasher(u64);
impl Hasher for IntHasher {
    fn finish(&self) -> u64 {
        self.0
    }
    fn write(&mut self, bytes: &[u8]) {
        for &b in bytes {
            self.0 = (self.0 ^ b as u64).wrapping_mul(0x0100_0000_01b3);
        }
    }
    fn write_u32(&mut self, i: u32) {
        self.0 = (i as u64).wrapping_mul(0x9E37_79B9_7F4A_7C15);
    }
}
type IntMap<K, V> = HashMap<K, V, BuildHasherDefault<IntHasher>>;

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
    fn new(symbols: Vec<i64>, freqs: Vec<u64>) -> Self {
        let n = symbols.len();
        let mut cum = vec![0u64; n + 1];
        for i in 0..n {
            cum[i + 1] = cum[i] + freqs[i];
        }
        let total = cum[n];
        let index = symbols.iter().enumerate().map(|(i, &s)| (s, i)).collect();
        FreqModel { symbols, freqs, cum, total, index }
    }
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
        FreqModel::new(symbols, freqs)
    }
    /// Build from raw `{symbol: count}` (every count >= 1), quantized to `TARGET_TOTAL`,
    /// symbols ascending — byte-identical to `freqmodel.FrequencyModel.from_counts`.
    fn from_counts(counts: &std::collections::BTreeMap<i64, u64>) -> Self {
        let raw_total: u64 = counts.values().sum();
        let symbols: Vec<i64> = counts.keys().copied().collect();
        let freqs: Vec<u64> = symbols
            .iter()
            .map(|s| (counts[s] * TARGET_TOTAL / raw_total).max(1))
            .collect();
        FreqModel::new(symbols, freqs)
    }
    fn serialize(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(4 + 8 * self.symbols.len());
        out.extend_from_slice(&(self.symbols.len() as u32).to_be_bytes());
        for (&s, &f) in self.symbols.iter().zip(&self.freqs) {
            out.extend_from_slice(&(s as u32).to_be_bytes());
            out.extend_from_slice(&(f as u32).to_be_bytes());
        }
        out
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
    index: IntMap<u16, Vec<usize>>, // 2-byte prefix -> pattern ids, longest-first
}
impl Dictionary {
    fn new(patterns: Vec<Vec<u8>>) -> Self {
        let mut index: IntMap<u16, Vec<usize>> = Default::default();
        for (pid, p) in patterns.iter().enumerate() {
            if p.len() >= 2 {
                index.entry((p[0] as u16) << 8 | p[1] as u16).or_default().push(pid);
            }
        }
        for bucket in index.values_mut() {
            bucket.sort_by(|&a, &b| patterns[b].len().cmp(&patterns[a].len()));
        }
        Dictionary { patterns, index }
    }
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
        Dictionary::new(patterns)
    }
    fn serialize(&self) -> Vec<u8> {
        let mut out = Vec::new();
        out.extend_from_slice(&(self.patterns.len() as u32).to_be_bytes());
        for p in &self.patterns {
            out.extend_from_slice(&(p.len() as u16).to_be_bytes());
            out.extend_from_slice(p);
        }
        out
    }
    /// Longest pattern that is a prefix of `data[pos..]` → (pid, length).
    fn matcher(&self, data: &[u8], pos: usize, min_match: usize) -> Option<(usize, usize)> {
        if pos + 2 > data.len() {
            return None;
        }
        let bucket = self.index.get(&((data[pos] as u16) << 8 | data[pos + 1] as u16))?;
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
const VERSION: u16 = 7;
const TARGET_TOTAL: u64 = 1 << 16;
const DECISION_CHAIN: usize = 16; // shallow depth for the use_lz validation decision
const LZ_OVER_DICT_MARGIN: usize = 4;

fn rep_init() -> Vec<i64> {
    (1..=REP_N as i64).collect()
}

#[inline]
fn max_len_slot() -> u32 {
    value_slot((MAX_MATCH - MIN_MATCH + 1) as u64).0
}
#[inline]
fn max_dist_slot() -> u32 {
    value_slot(WINDOW as u64).0
}

// Short LZ matches only pay off when nearby (the greedy parse's `_accept_lz`).
fn accept_lz(length: usize, distance: usize) -> bool {
    if length < MIN_MATCH {
        return false;
    }
    let cap = match length {
        3 => 1usize << 7,
        4 => 1usize << 11,
        5 => 1usize << 14,
        _ => return true,
    };
    distance <= cap
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
    fn save(&self) -> Vec<u8> {
        let mut out = Vec::new();
        out.extend_from_slice(b"CMP7");
        out.extend_from_slice(&self.version.to_be_bytes());
        out.push(self.use_lz as u8);
        let tid = self.type_id.as_bytes();
        out.push(tid.len() as u8);
        out.extend_from_slice(tid);
        for chunk in [
            self.dictionary.serialize(),
            self.main.serialize(),
            self.dist.serialize(),
            self.mode.serialize(),
            transform::serialize(&self.transform),
            self.blob.clone(),
        ] {
            out.extend_from_slice(&(chunk.len() as u32).to_be_bytes());
            out.extend_from_slice(&chunk);
        }
        out
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

/// Common-prefix length of `buf[i..]` and `buf[j..]`, capped at `limit`. Compares 8
/// bytes at a time (little-endian word + first-mismatch via the XOR's low set bit), so
/// long matches cost O(L/8); the result is identical to the byte-by-byte loop.
fn match_len(buf: &[u8], i: usize, j: usize, limit: usize) -> usize {
    let mut n = 0;
    while n + 8 <= limit {
        let a = u64::from_le_bytes(buf[i + n..i + n + 8].try_into().unwrap());
        let b = u64::from_le_bytes(buf[j + n..j + n + 8].try_into().unwrap());
        if a != b {
            return n + (a ^ b).trailing_zeros() as usize / 8;
        }
        n += 8;
    }
    while n < limit && buf[i + n] == buf[j + n] {
        n += 1;
    }
    n
}

#[inline]
fn key3(buf: &[u8], i: usize) -> u32 {
    (buf[i] as u32) << 16 | (buf[i + 1] as u32) << 8 | buf[i + 2] as u32
}

struct Forward {
    off: Vec<usize>,
    cand_len: Vec<usize>,
    cand_dist: Vec<usize>,
}

fn lz_forward(combined: &[u8], base: usize, max_chain: usize) -> Forward {
    let nn = combined.len();
    let mut head: IntMap<u32, usize> = Default::default();
    head.reserve(nn);
    let mut prev = vec![usize::MAX; nn];
    let insert = |head: &mut IntMap<u32, usize>, prev: &mut [usize], i: usize| {
        if i + MIN_MATCH <= nn {
            let key = key3(combined, i);
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
    // Per distinct match length keep the first-seen (smallest, since the chain runs
    // newest-first so distance increases monotonically) candidate, in first-appearance
    // order. A generation-stamped array does the dedup with no per-position allocation.
    let mut seen = vec![0u32; MAX_MATCH + 1];
    let mut gen = 0u32;
    for p in base..nn {
        off[p - base] = cand_len.len();
        if p + MIN_MATCH <= nn {
            gen += 1;
            let mut cand = *head.get(&key3(combined, p)).unwrap_or(&usize::MAX);
            let mut chain = max_chain;
            let limit = MAX_MATCH.min(nn - p);
            while cand != usize::MAX && p - cand <= WINDOW && chain > 0 {
                let length = match_len(combined, cand, p, limit);
                if length >= MIN_MATCH && seen[length] != gen {
                    seen[length] = gen;
                    cand_len.push(length);
                    cand_dist.push(p - cand);
                }
                cand = prev[cand];
                chain -= 1;
            }
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

fn tokenize_optimal(m: &Model, data: &[u8], max_chain: usize) -> Vec<Tok> {
    let base = m.blob.len();
    let combined: Vec<u8> = if base > 0 {
        let mut c = m.blob.clone();
        c.extend_from_slice(data);
        c
    } else {
        data.to_vec()
    };
    let nn = combined.len();
    let fwd = lz_forward(&combined, base, max_chain);
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

// Greedy lazy LZ+dict parse (the `tokenizer.tokenize` use_lz path) — used by training's
// provisional/decision passes. Byte-identical to the Python lazy walk.
enum Choice {
    Lit,
    Dict(usize, usize),  // pid, length
    Match(usize, usize), // length, distance
}
fn choice_cover(c: &Choice) -> usize {
    match c {
        Choice::Lit => 0,
        Choice::Dict(_, l) => *l,
        Choice::Match(l, _) => *l,
    }
}

fn find_lz(
    data: &[u8], pos: usize, head: &IntMap<u32, usize>, prev: &[usize], max_chain: usize,
) -> (usize, usize) {
    let n = data.len();
    let (mut best_len, mut best_dist) = (0usize, 0usize);
    if pos + MIN_MATCH <= n {
        let mut cand = *head.get(&key3(data, pos)).unwrap_or(&usize::MAX);
        let mut chain = max_chain;
        let limit = MAX_MATCH.min(n - pos);
        while cand != usize::MAX && pos - cand <= WINDOW && chain > 0 {
            let length = match_len(data, cand, pos, limit);
            if length > best_len {
                best_len = length;
                best_dist = pos - cand;
                if length == limit {
                    break;
                }
            }
            cand = prev[cand];
            chain -= 1;
        }
    }
    (best_len, best_dist)
}

fn decide(m: &Model, data: &[u8], pos: usize, best_len: usize, best_dist: usize) -> Choice {
    let dm = m.dictionary.matcher(data, pos, MIN_MATCH);
    let dict_len = dm.map(|(_, l)| l).unwrap_or(0);
    let use_dict = dict_len >= MIN_MATCH;
    let lz_ok = accept_lz(best_len, best_dist);
    if use_dict && (!lz_ok || best_len < dict_len + LZ_OVER_DICT_MARGIN) {
        return Choice::Dict(dm.unwrap().0, dict_len);
    }
    if lz_ok {
        return Choice::Match(best_len, best_dist);
    }
    Choice::Lit
}

fn tokenize_greedy(m: &Model, data: &[u8], max_chain: usize) -> Vec<Tok> {
    let base = m.blob.len();
    let combined: Vec<u8> = if base > 0 {
        let mut c = m.blob.clone();
        c.extend_from_slice(data);
        c
    } else {
        data.to_vec()
    };
    let nn = combined.len();
    let mut head: IntMap<u32, usize> = Default::default();
    head.reserve(nn);
    let mut prev = vec![usize::MAX; nn];
    let insert = |head: &mut IntMap<u32, usize>, prev: &mut [usize], i: usize| {
        if i + MIN_MATCH <= nn {
            let key = key3(&combined, i);
            prev[i] = *head.get(&key).unwrap_or(&usize::MAX);
            head.insert(key, i);
        }
    };
    for i in 0..base {
        insert(&mut head, &mut prev, i);
    }
    let choice = |head: &IntMap<u32, usize>, prev: &[usize], at: usize| -> Choice {
        let (bl, bd) = find_lz(&combined, at, head, prev, max_chain);
        decide(m, &combined, at, bl, bd)
    };

    let mut tokens = Vec::new();
    let mut pos = base;
    let mut pending: Option<Choice> = None;
    while pos < nn {
        let tok = pending.take().unwrap_or_else(|| choice(&head, &prev, pos));
        match tok {
            Choice::Lit => {
                tokens.push(Tok::Lit(combined[pos]));
                insert(&mut head, &mut prev, pos);
                pos += 1;
            }
            Choice::Dict(pid, length) => {
                tokens.push(Tok::Dict(pid));
                for i in pos..pos + length {
                    insert(&mut head, &mut prev, i);
                }
                pos += length;
            }
            Choice::Match(length, dist) => {
                insert(&mut head, &mut prev, pos); // so the lookahead at pos+1 sees pos
                if pos + 1 < nn {
                    let nxt = choice(&head, &prev, pos + 1);
                    if choice_cover(&nxt) > length {
                        tokens.push(Tok::Lit(combined[pos]));
                        pending = Some(nxt);
                        pos += 1;
                        continue;
                    }
                }
                tokens.push(Tok::Match(length, dist));
                for i in pos + 1..pos + length {
                    insert(&mut head, &mut prev, i);
                }
                pos += length;
            }
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

// ============================================================================
// Training — byte-identical to model.py / dictionary.py / freqmodel.py, except the
// transform selector's zlib proxy (flate2 ≠ CPython zlib, so its choice can differ on
// borderline numeric data; the resulting model is still valid + cross-loadable).
// ============================================================================

use std::collections::BTreeMap;

/// Walk a token sequence with the repeat-offset cache, invoking `f(table, sym, extra)` —
/// table 0=main / 1=dist / 2=mode. Mirrors `model._rep_stream`; shared by counting + pricing.
fn for_each_sym(tokens: &[Tok], len_base: i64, mut f: impl FnMut(u8, i64, u32)) {
    let mut reps = rep_init();
    for tok in tokens {
        match tok {
            Tok::Lit(b) => f(0, *b as i64, 0),
            Tok::Dict(pid) => f(0, 256 + *pid as i64, 0),
            Tok::Match(length, dist) => {
                let (lslot, _) = value_slot((length - MIN_MATCH + 1) as u64);
                f(0, len_base + lslot as i64, lslot);
                let d = *dist as i64;
                if let Some(i) = reps.iter().position(|&r| r == d) {
                    f(2, (i + 1) as i64, 0);
                    reps.remove(i);
                } else {
                    f(2, MODE_NORMAL, 0);
                    let (dslot, _) = value_slot(*dist as u64);
                    f(1, dslot as i64, dslot);
                    reps.pop();
                }
                reps.insert(0, d);
            }
        }
    }
}

/// Baseline count of 1 for every symbol that could ever be emitted (the losslessness floor).
fn baseline_counts(
    n_patterns: usize, len_base: i64,
) -> (BTreeMap<i64, u64>, BTreeMap<i64, u64>, BTreeMap<i64, u64>) {
    let mut main = BTreeMap::new();
    let mut dist = BTreeMap::new();
    let mut mode = BTreeMap::new();
    for b in 0..256i64 {
        main.insert(b, 1);
    }
    for pid in 0..n_patterns as i64 {
        main.insert(256 + pid, 1);
    }
    for slot in 0..=max_len_slot() as i64 {
        main.insert(len_base + slot, 1);
    }
    for slot in 0..=max_dist_slot() as i64 {
        dist.insert(slot, 1);
    }
    for m in 0..=REP_N as i64 {
        mode.insert(m, 1);
    }
    (main, dist, mode)
}

fn models_from_tokenized(
    tokenized: &[Vec<Tok>], n_patterns: usize, len_base: i64,
) -> (FreqModel, FreqModel, FreqModel) {
    let (mut main, mut dist, mut mode) = baseline_counts(n_patterns, len_base);
    for toks in tokenized {
        for_each_sym(toks, len_base, |t, sym, _| {
            let m = match t {
                0 => &mut main,
                1 => &mut dist,
                _ => &mut mode,
            };
            *m.entry(sym).or_insert(0) += 1;
        });
    }
    (
        FreqModel::from_counts(&main),
        FreqModel::from_counts(&dist),
        FreqModel::from_counts(&mode),
    )
}

// --- dictionary mining -------------------------------------------------------

fn mine_patterns(samples: &[&[u8]], max_patterns: usize, min_len: usize, max_len: usize) -> Dictionary {
    const MAX_MINING: usize = 1_000_000;
    const DMER: usize = 8;
    let mut blob: Vec<u8> = samples.concat();
    if blob.len() > MAX_MINING {
        blob.truncate(MAX_MINING);
    }
    let n = blob.len();
    let d = DMER.min(min_len.max(1));
    const DEFAULT_LENGTHS: [usize; 15] =
        [3, 4, 5, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256];
    let mut lengths: Vec<usize> =
        DEFAULT_LENGTHS.iter().copied().filter(|&l| min_len <= l && l <= max_len).collect();
    if lengths.is_empty() {
        lengths = vec![min_len];
    }

    let mut dmer_freq: HashMap<&[u8], u64> = HashMap::new();
    if n >= d {
        for i in 0..=n - d {
            *dmer_freq.entry(&blob[i..i + d]).or_insert(0) += 1;
        }
    }
    let mut counts: HashMap<&[u8], u64> = HashMap::new();
    for &length in &lengths {
        if n < length {
            continue;
        }
        if length >= d {
            for i in 0..=n - length {
                if dmer_freq.get(&blob[i..i + d]).copied().unwrap_or(0) >= 2 {
                    *counts.entry(&blob[i..i + length]).or_insert(0) += 1;
                }
            }
        } else {
            for i in 0..=n - length {
                *counts.entry(&blob[i..i + length]).or_insert(0) += 1;
            }
        }
    }

    let mut scored: Vec<(u64, &[u8])> = Vec::new();
    for (&pat, &freq) in &counts {
        if freq < 2 {
            continue;
        }
        if pat.len() <= 2 {
            continue; // saving = len - _REFERENCE_COST(2) must be > 0
        }
        scored.push((freq * (pat.len() as u64 - 2), pat));
    }
    // score desc, then length desc, then pattern bytes ascending — a total order, so the
    // result is deterministic regardless of HashMap iteration order.
    scored.sort_by(|a, b| {
        b.0.cmp(&a.0)
            .then_with(|| b.1.len().cmp(&a.1.len()))
            .then_with(|| a.1.cmp(b.1))
    });
    let patterns: Vec<Vec<u8>> =
        scored.iter().take(max_patterns).map(|(_, p)| p.to_vec()).collect();
    Dictionary::new(patterns)
}

// --- LZ blob (COVER coverage selection) -------------------------------------

fn build_blob(samples: &[&[u8]], cap: usize) -> Vec<u8> {
    const D: usize = 8;
    const SEG: usize = 2048;
    const STRIDE: usize = 512;
    const MAX_BYTES: usize = 1_000_000;
    let mut src: Vec<u8> = samples.concat();
    if src.len() > MAX_BYTES {
        src.truncate(MAX_BYTES);
    }
    let n = src.len();
    if n <= cap {
        return src;
    }
    let mut dmer_freq: HashMap<&[u8], i64> = HashMap::new();
    for i in 0..=n - D {
        *dmer_freq.entry(&src[i..i + D]).or_insert(0) += 1;
    }
    let mut candidates: Vec<(i64, usize, usize)> = Vec::new();
    let mut start = 0;
    while start <= n - D {
        let length = SEG.min(n - start);
        if length >= D {
            let mut score = 0i64;
            for j in start..=start + length - D {
                score += dmer_freq[&src[j..j + D]];
            }
            candidates.push((score, start, length));
        }
        start += STRIDE;
    }
    candidates.sort_by(|a, b| b.0.cmp(&a.0)); // -score; stable keeps start ascending on ties

    let mut selected: Vec<Vec<u8>> = Vec::new();
    let mut total = 0usize;
    for (_score, start, length) in candidates {
        if total >= cap {
            break;
        }
        let mut s = 0i64;
        for j in start..=start + length - D {
            s += dmer_freq[&src[j..j + D]];
        }
        if s <= 0 {
            continue; // already covered by earlier picks
        }
        let mut piece = src[start..start + length].to_vec();
        if total + piece.len() > cap {
            piece.truncate(cap - total);
        }
        total += piece.len();
        selected.push(piece);
        for j in start..=start + length - D {
            *dmer_freq.get_mut(&src[j..j + D]).unwrap() = 0;
        }
    }
    selected.reverse(); // most valuable (first selected) nearest the data
    selected.concat()
}

fn build_blob_naive(samples: &[&[u8]], cap: usize) -> Vec<u8> {
    let mut blob = Vec::new();
    for s in samples {
        if blob.len() >= cap {
            break;
        }
        blob.extend_from_slice(s);
    }
    blob.truncate(cap);
    blob
}

fn blob_for(spec: (u8, usize), samples: &[&[u8]]) -> Vec<u8> {
    match spec.0 {
        0 => Vec::new(),                          // none
        1 => build_blob_naive(samples, spec.1),   // naive
        _ => build_blob(samples, spec.1),         // cover
    }
}

fn blob_specs() -> Vec<(u8, usize)> {
    vec![
        (0, 0),
        (1, 1 << 15),
        (2, 1 << 15),
        (2, 1 << 16),
        (2, 1 << 17),
        (1, 1 << 17),
        (2, 1 << 18),
        (2, 1 << 19),
        (1, 1 << 19),
    ]
}

// --- artifacts / pricing / search -------------------------------------------

fn empty_model(dictionary: Dictionary, blob: Vec<u8>, use_lz: bool) -> Model {
    Model {
        type_id: String::new(),
        version: VERSION,
        use_lz,
        dictionary,
        main: FreqModel::new(vec![], vec![]),
        dist: FreqModel::new(vec![], vec![]),
        mode: FreqModel::new(vec![], vec![]),
        transform: vec![],
        blob,
    }
}

fn artifacts(
    samples: &[&[u8]], blob: &[u8], max_patterns: usize, min_len: usize, max_len: usize,
    max_chain: usize,
) -> (Dictionary, FreqModel, FreqModel, FreqModel) {
    let use_lz = !blob.is_empty();
    let dictionary = mine_patterns(samples, max_patterns, min_len, max_len);
    let n_patterns = dictionary.patterns.len();
    let len_base = 256 + n_patterns as i64;
    let mut m = empty_model(dictionary, blob.to_vec(), use_lz);
    let tokenized: Vec<Vec<Tok>> = if use_lz {
        // bootstrap costs from a fast lazy parse, then one cost-optimal re-parse
        let prov: Vec<Vec<Tok>> =
            samples.iter().map(|s| tokenize_greedy(&m, s, DECISION_CHAIN)).collect();
        let (pm, pd, pmode) = models_from_tokenized(&prov, n_patterns, len_base);
        m.main = pm;
        m.dist = pd;
        m.mode = pmode;
        samples.iter().map(|s| tokenize_optimal(&m, s, max_chain)).collect()
    } else {
        samples.iter().map(|s| tokenize_dict_only(&m, s)).collect()
    };
    let (main, dist, mode) = models_from_tokenized(&tokenized, n_patterns, len_base);
    (m.dictionary, main, dist, mode)
}

fn price(
    samples: &[&[u8]], dictionary: Dictionary, blob: Vec<u8>, main: FreqModel, dist: FreqModel,
    mode: FreqModel, max_chain: usize,
) -> f64 {
    let use_lz = !blob.is_empty();
    let m = Model {
        type_id: String::new(),
        version: VERSION,
        use_lz,
        dictionary,
        main,
        dist,
        mode,
        transform: vec![],
        blob,
    };
    let len_base = m.len_base();
    let mut bits = 0f64;
    for s in samples {
        let toks = if use_lz {
            tokenize_optimal(&m, s, max_chain)
        } else {
            tokenize_dict_only(&m, s)
        };
        for_each_sym(&toks, len_base, |t, sym, extra| {
            let fm = match t {
                0 => &m.main,
                1 => &m.dist,
                _ => &m.mode,
            };
            bits += fm.cost_bits(sym) + extra as f64;
        });
    }
    bits
}

fn search_costs(
    specs: &[(u8, usize)], fit: &[&[u8]], val: &[&[u8]], max_patterns: usize, min_len: usize,
    max_len: usize,
) -> Vec<f64> {
    specs
        .iter()
        .map(|&spec| {
            let blob = blob_for(spec, fit);
            let (d, mm, dm, mo) =
                artifacts(fit, &blob, max_patterns, min_len, max_len, DECISION_CHAIN);
            price(val, d, blob, mm, dm, mo, DECISION_CHAIN)
        })
        .collect()
}

/// Train a model from byte samples — byte-identical to `model.train` where the zlib transform
/// proxy agrees (text/image/float), valid + cross-loadable otherwise.
pub fn train(
    samples: &[&[u8]], type_id: &str, max_patterns: usize, min_len: usize, max_len: usize,
) -> Vec<u8> {
    let tspec = transform::select(samples);
    let tsamples: Vec<Vec<u8>> = samples.iter().map(|s| transform::apply(s, &tspec)).collect();
    let refs: Vec<&[u8]> = tsamples.iter().map(|v| v.as_slice()).collect();

    let (fit, val): (&[&[u8]], &[&[u8]]) = if refs.len() >= 5 {
        let cut = (refs.len() * 4 / 5).max(1);
        (&refs[..cut], &refs[cut..])
    } else {
        (&refs[..], &refs[..])
    };

    let specs = blob_specs();
    let costs = search_costs(&specs, fit, val, max_patterns, min_len, max_len);
    let mut best_cost: Option<f64> = None;
    let mut best_spec = (0u8, 0usize);
    for (&spec, &cost) in specs.iter().zip(&costs) {
        if best_cost.map_or(true, |b| cost < b) {
            best_cost = Some(cost);
            best_spec = spec;
        }
    }

    let blob = blob_for(best_spec, &refs);
    let (dictionary, main, dist, mode) =
        artifacts(&refs, &blob, max_patterns, min_len, max_len, MAX_CHAIN);
    let model = Model {
        type_id: type_id.to_string(),
        version: VERSION,
        use_lz: !blob.is_empty(),
        dictionary,
        main,
        dist,
        mode,
        transform: tspec,
        blob,
    };
    model.save()
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

/// Per-file hash-chain depth — deep on small inputs (cheap, ~1% denser), tapering to
/// `MAX_CHAIN` on large ones (bounded cost). Always `>= MAX_CHAIN`, so never worse than the
/// fixed default. Byte-for-byte the same rule as `tokenizer.adaptive_max_chain`.
fn adaptive_max_chain(n: usize) -> usize {
    const ADAPT_MAX: usize = 2048;
    const ADAPT_BUDGET: usize = 2048 * 2048;
    (ADAPT_BUDGET / n.max(1)).clamp(MAX_CHAIN, ADAPT_MAX)
}

pub fn compress(model_blob: &[u8], data: &[u8]) -> Vec<u8> {
    let m = Model::load(model_blob);
    let tdata = transform::apply(data, &m.transform);
    let tokens = if m.use_lz {
        tokenize_optimal(&m, &tdata, adaptive_max_chain(tdata.len()))
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

/// Train a model from `n` samples (flat `data` split by `lens`), returning a saved CMP7 blob.
#[no_mangle]
pub unsafe extern "C" fn train_model(
    data: *const u8, lens: *const i64, n: i64, type_id: *const u8, tid_len: i64,
    max_patterns: i64, min_len: i64, max_len: i64, out: *mut u8, cap: i64,
) -> i64 {
    let lens = std::slice::from_raw_parts(lens, n as usize);
    let total: i64 = lens.iter().sum();
    let flat = std::slice::from_raw_parts(data, total as usize);
    let mut samples: Vec<&[u8]> = Vec::with_capacity(n as usize);
    let mut off = 0usize;
    for &l in lens {
        samples.push(&flat[off..off + l as usize]);
        off += l as usize;
    }
    let tid = std::str::from_utf8(std::slice::from_raw_parts(type_id, tid_len as usize)).unwrap();
    let blob = train(&samples, tid, max_patterns as usize, min_len as usize, max_len as usize);
    if blob.len() as i64 > cap {
        return -1;
    }
    std::ptr::copy_nonoverlapping(blob.as_ptr(), out, blob.len());
    blob.len() as i64
}
