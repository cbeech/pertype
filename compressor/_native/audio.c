/* Native hot loops (compiled to audio.so by native.py, called via ctypes).
 *
 * Each function must produce bit-identical output to its pure-Python reference
 * (in audiocodec.py / transform.py), or losslessness breaks. Compiled with
 * -fwrapv (signed wrap like numpy int64) and -ffp-contract=off (no FMA, so the
 * float Rice `run` update matches Python).
 *
 * Contents: the lossless audio codec's predictor + Rice coder, and the byte-
 * stream `delta` transform used by the image/numeric path.
 */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

static inline int64_t sgn(int64_t v) { return (v > 0) - (v < 0); }

void lms_fwd(const int64_t *x, int64_t *out, long n, int taps, int shift) {
    int64_t *w = (int64_t *)calloc(taps, sizeof(int64_t));
    int64_t *h = (int64_t *)calloc(taps, sizeof(int64_t));
    for (long i = 0; i < n; i++) {
        int64_t sum = 0;
        for (int j = 0; j < taps; j++) sum += w[j] * h[j];
        int64_t pred = sum >> shift;          /* arithmetic shift = floor, matches Python */
        int64_t err = x[i] - pred;
        out[i] = err;
        if (err > 0)      { for (int j = 0; j < taps; j++) w[j] += sgn(h[j]); }
        else if (err < 0) { for (int j = 0; j < taps; j++) w[j] -= sgn(h[j]); }
        for (int j = taps - 1; j > 0; j--) h[j] = h[j - 1];
        h[0] = x[i];
    }
    free(w); free(h);
}

void lms_inv(const int64_t *e, int64_t *x, long n, int taps, int shift) {
    int64_t *w = (int64_t *)calloc(taps, sizeof(int64_t));
    int64_t *h = (int64_t *)calloc(taps, sizeof(int64_t));
    for (long i = 0; i < n; i++) {
        int64_t sum = 0;
        for (int j = 0; j < taps; j++) sum += w[j] * h[j];
        int64_t pred = sum >> shift;
        int64_t xi = e[i] + pred;
        x[i] = xi;
        if (e[i] > 0)      { for (int j = 0; j < taps; j++) w[j] += sgn(h[j]); }
        else if (e[i] < 0) { for (int j = 0; j < taps; j++) w[j] -= sgn(h[j]); }
        for (int j = taps - 1; j > 0; j--) h[j] = h[j - 1];
        h[0] = xi;
    }
    free(w); free(h);
}

/* order-2 fixed predictor (inverse is a sequential recurrence) */
void fixed2_fwd(const int64_t *x, int64_t *e, long n) {
    e[0] = (n > 0) ? x[0] : 0;
    if (n > 1) e[1] = x[1];
    for (long i = 2; i < n; i++) e[i] = x[i] - (2 * x[i - 1] - x[i - 2]);
}

void fixed2_inv(const int64_t *e, int64_t *x, long n) {
    x[0] = (n > 0) ? e[0] : 0;
    if (n > 1) x[1] = e[1];
    for (long i = 2; i < n; i++) x[i] = e[i] + 2 * x[i - 1] - x[i - 2];
}

/* --- adaptive Rice coding (MSB-first bits, matching bitio.py) --------------- */
#define RICE_ALPHA 0.02

static int _k_from_run(double run) {
    long v = (long)run;                 /* trunc toward zero, like int(run) */
    if (v < 1) return 0;
    return 63 - __builtin_clzll((unsigned long long)v);   /* bit_length(v) - 1 */
}

/* Returns bytes written, or -1 if the output buffer is too small. */
long rice_encode(const int64_t *res, long n, uint8_t *out, long cap) {
    double run = 16.0;
    long byte = 0;
    int nbits = 0;
    unsigned cur = 0;
    for (long i = 0; i < n; i++) {
        int64_t r = res[i];
        uint64_t u = (uint64_t)((r << 1) ^ (r >> 63));   /* zigzag */
        int k = _k_from_run(run);
        uint64_t q = u >> k;
        for (uint64_t t = 0; t < q + 1; t++) {           /* q ones then a zero */
            cur = (cur << 1) | (t < q ? 1u : 0u);
            if (++nbits == 8) { if (byte >= cap) return -1; out[byte++] = cur; cur = 0; nbits = 0; }
        }
        for (int s = k - 1; s >= 0; s--) {               /* k remainder bits, MSB first */
            cur = (cur << 1) | (unsigned)((u >> s) & 1);
            if (++nbits == 8) { if (byte >= cap) return -1; out[byte++] = cur; cur = 0; nbits = 0; }
        }
        run += (u - run) * RICE_ALPHA;
    }
    if (nbits > 0) { if (byte >= cap) return -1; out[byte++] = (uint8_t)(cur << (8 - nbits)); }
    return byte;
}

void rice_decode(const uint8_t *in, long n, int64_t *out) {
    double run = 16.0;
    long pos = 0;                                        /* bit position */
    for (long i = 0; i < n; i++) {
        int k = _k_from_run(run);
        uint64_t q = 0;
        while ((in[pos >> 3] >> (7 - (pos & 7))) & 1) { q++; pos++; }
        pos++;                                           /* the terminating zero */
        uint64_t rem = 0;
        for (int s = 0; s < k; s++) { rem = (rem << 1) | ((in[pos >> 3] >> (7 - (pos & 7))) & 1); pos++; }
        uint64_t u = (q << k) | rem;
        out[i] = (int64_t)(u >> 1) ^ -(int64_t)(u & 1);  /* unzigzag */
        run += (u - run) * RICE_ALPHA;
    }
}

/* --- byte-stream delta transform (image/numeric path) ----------------------- */
void delta_fwd(const uint8_t *data, uint8_t *out, long n, int stride) {
    for (long i = 0; i < stride && i < n; i++) out[i] = data[i];
    for (long i = stride; i < n; i++) out[i] = (uint8_t)(data[i] - data[i - stride]);
}

void delta_inv(const uint8_t *data, uint8_t *out, long n, int stride) {
    for (long i = 0; i < stride && i < n; i++) out[i] = data[i];
    for (long i = stride; i < n; i++) out[i] = (uint8_t)(out[i - stride] + data[i]);
}

/* --- context-adaptive arithmetic residual coder (mirrors ctxcoder.py) ------
 *
 * Witten–Neal–Cleary 32-bit arithmetic coder driving a per-context adaptive
 * model over magnitude buckets, with raw mantissa bits coded as uniform symbols
 * through the same coder. Bit output is MSB-first with a zero-padded final byte,
 * exactly matching bitio.BitWriter, so the C output is byte-identical to the
 * pure-Python reference and files are interchangeable. All integer math.
 */
#define CTX_NB 65               /* buckets 0..64 cover any int64 zigzag magnitude */
#define CTX_CLAMP 16            /* context bucket clamp (keeps the model dense) */
#define CTX_NCTX ((CTX_CLAMP + 1) * (CTX_CLAMP + 1))   /* order-2: (prev, prev-prev) */
#define CTX_INCR 32
#define CTX_RESCALE (1 << 14)
#define AC_HALF      0x80000000ULL
#define AC_QUARTER   0x40000000ULL
#define AC_3QUARTER  0xC0000000ULL
#define AC_MAX       0xFFFFFFFFULL

/* MSB-first bit writer */
typedef struct { uint8_t *out; long cap, byte; unsigned cur; int nbits, overflow; } bitw;

static inline void bw_bit(bitw *w, int bit) {
    w->cur = (w->cur << 1) | (unsigned)(bit & 1);
    if (++w->nbits == 8) {
        if (w->byte >= w->cap) { w->overflow = 1; w->nbits = 0; w->cur = 0; return; }
        w->out[w->byte++] = (uint8_t)w->cur; w->cur = 0; w->nbits = 0;
    }
}

typedef struct { uint64_t low, high; long pending; bitw *w; } aenc;

static inline void ae_emit(aenc *e, int bit) {
    bw_bit(e->w, bit);
    while (e->pending) { bw_bit(e->w, bit ^ 1); e->pending--; }
}

static void ae_encode(aenc *e, uint64_t cum, uint64_t freq, uint64_t total) {
    uint64_t span = e->high - e->low + 1;
    e->high = e->low + span * (cum + freq) / total - 1;
    e->low  = e->low + span * cum / total;
    for (;;) {
        if (e->high < AC_HALF) ae_emit(e, 0);
        else if (e->low >= AC_HALF) { ae_emit(e, 1); e->low -= AC_HALF; e->high -= AC_HALF; }
        else if (e->low >= AC_QUARTER && e->high < AC_3QUARTER) {
            e->pending++; e->low -= AC_QUARTER; e->high -= AC_QUARTER;
        } else break;
        e->low <<= 1; e->high = (e->high << 1) | 1;
    }
}

static void ctx_init(int freq[CTX_NCTX][CTX_NB], long tot[CTX_NCTX]) {
    for (int c = 0; c < CTX_NCTX; c++) {
        for (int s = 0; s < CTX_NB; s++) freq[c][s] = 1;
        tot[c] = CTX_NB;
    }
}

static inline int ctx_index(int pk, int pk2) {
    int a = pk < CTX_CLAMP ? pk : CTX_CLAMP;
    int b = pk2 < CTX_CLAMP ? pk2 : CTX_CLAMP;
    return a * (CTX_CLAMP + 1) + b;
}

static void ctx_bump(int *f, long *tot, int k) {
    f[k] += CTX_INCR; *tot += CTX_INCR;
    if (*tot >= CTX_RESCALE) {
        long t = 0;
        for (int s = 0; s < CTX_NB; s++) { f[s] = (f[s] + 1) >> 1; t += f[s]; }
        *tot = t;
    }
}

/* Returns bytes written, or -1 if the output buffer is too small. */
long ctx_encode(const int64_t *res, long n, uint8_t *out, long cap) {
    int freq[CTX_NCTX][CTX_NB]; long tot[CTX_NCTX];
    ctx_init(freq, tot);
    bitw w = { out, cap, 0, 0, 0, 0 };
    aenc e = { 0, AC_MAX, 0, &w };
    int pk = 0, pk2 = 0;
    for (long i = 0; i < n; i++) {
        int64_t r = res[i];
        uint64_t u = (((uint64_t)r) << 1) ^ (uint64_t)(r >> 63);   /* zigzag */
        int k = u ? (64 - __builtin_clzll(u)) : 0;
        int ctx = ctx_index(pk, pk2);
        int *f = freq[ctx];
        uint64_t cum = 0;
        for (int s = 0; s < k; s++) cum += (uint64_t)f[s];
        ae_encode(&e, cum, (uint64_t)f[k], (uint64_t)tot[ctx]);
        if (k >= 2) {
            uint64_t mant = u & ((1ULL << (k - 1)) - 1);
            for (int shift = k - 2; shift >= 0; shift--)
                ae_encode(&e, (mant >> shift) & 1, 1, 2);
        }
        if (w.overflow) return -1;
        ctx_bump(f, &tot[ctx], k);
        pk2 = pk; pk = k;
    }
    e.pending++;                                   /* finish() */
    ae_emit(&e, e.low < AC_QUARTER ? 0 : 1);
    if (w.overflow) return -1;
    if (w.nbits > 0) {                             /* getvalue(): pad final byte */
        if (w.byte >= w.cap) return -1;
        w.out[w.byte++] = (uint8_t)(w.cur << (8 - w.nbits));
    }
    return w.byte;
}

typedef struct { uint64_t low, high, code; const uint8_t *in; long len, pos; } adec;

static inline int ad_bit(adec *d) {
    long bi = d->pos >> 3;
    int b = (bi >= d->len) ? 0 : ((d->in[bi] >> (7 - (d->pos & 7))) & 1);
    d->pos++;
    return b;
}

static uint64_t ad_target(adec *d, uint64_t total) {
    uint64_t span = d->high - d->low + 1;
    return ((d->code - d->low + 1) * total - 1) / span;
}

static void ad_update(adec *d, uint64_t cum, uint64_t freq, uint64_t total) {
    uint64_t span = d->high - d->low + 1;
    d->high = d->low + span * (cum + freq) / total - 1;
    d->low  = d->low + span * cum / total;
    for (;;) {
        if (d->high < AC_HALF) {}
        else if (d->low >= AC_HALF) { d->low -= AC_HALF; d->high -= AC_HALF; d->code -= AC_HALF; }
        else if (d->low >= AC_QUARTER && d->high < AC_3QUARTER) {
            d->low -= AC_QUARTER; d->high -= AC_QUARTER; d->code -= AC_QUARTER;
        } else break;
        d->low <<= 1; d->high = (d->high << 1) | 1; d->code = (d->code << 1) | (uint64_t)ad_bit(d);
    }
}

void ctx_decode(const uint8_t *in, long len, long n, int64_t *out) {
    int freq[CTX_NCTX][CTX_NB]; long tot[CTX_NCTX];
    ctx_init(freq, tot);
    adec d = { 0, AC_MAX, 0, in, len, 0 };
    for (int i = 0; i < 32; i++) d.code = (d.code << 1) | (uint64_t)ad_bit(&d);
    int pk = 0, pk2 = 0;
    for (long i = 0; i < n; i++) {
        int ctx = ctx_index(pk, pk2);
        int *f = freq[ctx];
        uint64_t total = (uint64_t)tot[ctx];
        uint64_t target = ad_target(&d, total);
        uint64_t cum = 0; int k = 0;
        while (cum + (uint64_t)f[k] <= target) { cum += (uint64_t)f[k]; k++; }
        ad_update(&d, cum, (uint64_t)f[k], total);
        uint64_t u;
        if (k == 0) u = 0;
        else if (k == 1) u = 1;
        else {
            uint64_t mant = 0;
            for (int j = 0; j < k - 1; j++) {
                int bit = (ad_target(&d, 2) >= 1) ? 1 : 0;
                ad_update(&d, (uint64_t)bit, 1, 2);
                mant = (mant << 1) | (uint64_t)bit;
            }
            u = (1ULL << (k - 1)) | mant;
        }
        out[i] = (int64_t)(u >> 1) ^ -(int64_t)(u & 1);    /* unzigzag */
        ctx_bump(f, &tot[ctx], k);
        pk2 = pk; pk = k;
    }
}

/* --- LZ token-stream coder for codec.py (mirrors codec._encode/_decode_tokens)
 *
 * Drives the same WNC arithmetic coder with three static frequency models
 * (main / dist / mode), each passed as a prefix-sum array `cum` of length n+1
 * (total == cum[n]); the models' symbol alphabets are contiguous 0..n-1, so the
 * symbol value indexes `cum` directly. Length/distance slot "extra" bits are
 * coded as uniform symbols through the coder, and the repeat-offset cache is
 * maintained identically here, so the output is byte-identical to the Python
 * reference and the streams are interchangeable.
 */
#define LZ_REP_N 3

static void model_encode(aenc *e, const int *cum, int n, int s) {
    ae_encode(e, (uint64_t)cum[s], (uint64_t)(cum[s + 1] - cum[s]), (uint64_t)cum[n]);
}

static int model_decode(adec *d, const int *cum, int n) {
    uint64_t total = (uint64_t)cum[n];
    uint64_t target = ad_target(d, total);
    int lo = 0, hi = n + 1;                 /* bisect_right(cum, target) */
    while (lo < hi) {
        int mid = (lo + hi) >> 1;
        if ((uint64_t)cum[mid] <= target) lo = mid + 1; else hi = mid;
    }
    int s = lo - 1;
    ad_update(d, (uint64_t)cum[s], (uint64_t)(cum[s + 1] - cum[s]), total);
    return s;
}

static void enc_bits(aenc *e, uint64_t value, int nbits) {
    for (int shift = nbits - 1; shift >= 0; shift--)
        ae_encode(e, (value >> shift) & 1, 1, 2);
}

static uint64_t dec_bits(adec *d, int nbits) {
    uint64_t v = 0;
    for (int i = 0; i < nbits; i++) {
        int bit = (ad_target(d, 2) >= 1) ? 1 : 0;
        ad_update(d, (uint64_t)bit, 1, 2);
        v = (v << 1) | (uint64_t)bit;
    }
    return v;
}

/* reps: pop element at index p, then insert `distance` at the front (len stays 3) */
static void rep_update(int64_t *reps, int p, int64_t distance) {
    for (int j = p; j < LZ_REP_N - 1; j++) reps[j] = reps[j + 1];
    for (int j = LZ_REP_N - 1; j > 0; j--) reps[j] = reps[j - 1];
    reps[0] = distance;
}

long lz_encode(const int *kind, const int64_t *aval, const int64_t *bval, long n_tokens,
               const int *mcum, int m_n, const int *dcum, int d_n, const int *ocum, int o_n,
               int len_base, int min_match, uint8_t *out, long cap) {
    bitw w = { out, cap, 0, 0, 0, 0 };
    aenc e = { 0, AC_MAX, 0, &w };
    int64_t reps[LZ_REP_N] = { 1, 2, 3 };
    for (long i = 0; i < n_tokens; i++) {
        int k = kind[i];
        if (k == 0) {                                   /* literal */
            model_encode(&e, mcum, m_n, (int)aval[i]);
        } else if (k == 1) {                            /* dict ref */
            model_encode(&e, mcum, m_n, 256 + (int)aval[i]);
        } else {                                        /* match */
            int64_t length = aval[i], distance = bval[i];
            int64_t v = length - min_match + 1;
            int lslot = 63 - __builtin_clzll((uint64_t)v);
            model_encode(&e, mcum, m_n, len_base + lslot);
            enc_bits(&e, (uint64_t)(v - ((int64_t)1 << lslot)), lslot);
            int ri = -1;
            for (int j = 0; j < LZ_REP_N; j++) if (reps[j] == distance) { ri = j; break; }
            if (ri >= 0) {
                model_encode(&e, ocum, o_n, ri + 1);
            } else {
                model_encode(&e, ocum, o_n, 0);         /* MODE_NORMAL */
                int dslot = 63 - __builtin_clzll((uint64_t)distance);
                model_encode(&e, dcum, d_n, dslot);
                enc_bits(&e, (uint64_t)(distance - ((int64_t)1 << dslot)), dslot);
            }
            rep_update(reps, ri >= 0 ? ri : LZ_REP_N - 1, distance);
        }
        if (w.overflow) return -1;
    }
    e.pending++;
    ae_emit(&e, e.low < AC_QUARTER ? 0 : 1);
    if (w.overflow) return -1;
    if (w.nbits > 0) {
        if (w.byte >= w.cap) return -1;
        w.out[w.byte++] = (uint8_t)(w.cur << (8 - w.nbits));
    }
    return w.byte;
}

void lz_decode(const uint8_t *in, long len, long n_tokens,
               const int *mcum, int m_n, const int *dcum, int d_n, const int *ocum, int o_n,
               int len_base, int n_patterns, int min_match,
               int *kind, int64_t *aval, int64_t *bval) {
    adec d = { 0, AC_MAX, 0, in, len, 0 };
    for (int i = 0; i < 32; i++) d.code = (d.code << 1) | (uint64_t)ad_bit(&d);
    int64_t reps[LZ_REP_N] = { 1, 2, 3 };
    for (long i = 0; i < n_tokens; i++) {
        int sym = model_decode(&d, mcum, m_n);
        if (sym < 256) {
            kind[i] = 0; aval[i] = sym; bval[i] = 0;
        } else if (sym < 256 + n_patterns) {
            kind[i] = 1; aval[i] = sym - 256; bval[i] = 0;
        } else {
            int lslot = sym - len_base;
            uint64_t lextra = dec_bits(&d, lslot);
            int64_t length = ((int64_t)1 << lslot) + (int64_t)lextra + min_match - 1;
            int m = model_decode(&d, ocum, o_n);
            int64_t distance;
            int p;
            if (m == 0) {
                int dslot = model_decode(&d, dcum, d_n);
                uint64_t dextra = dec_bits(&d, dslot);
                distance = ((int64_t)1 << dslot) + (int64_t)dextra;
                p = LZ_REP_N - 1;
            } else {
                distance = reps[m - 1];
                p = m - 1;
            }
            rep_update(reps, p, distance);
            kind[i] = 2; aval[i] = length; bval[i] = distance;
        }
    }
}

/* --- causal MED reconstruction (mirrors videocodec._med_fill) --------------
 *
 * For each intra pixel in raster order, predict from already-reconstructed
 * neighbours (left a, above b, above-left c) with the JPEG-LS / LOCO-I median,
 * then add the residual. Non-intra pixels are left as the caller filled them
 * (skip/inter), so neighbours read by an intra pixel are always final. rec is
 * modified in place. Integer-exact, so byte-identical to the Python loop. */
void med_fill(int64_t *rec, const uint8_t *intra, const int64_t *residual,
              long H, long W) {
    for (long y = 0; y < H; y++) {
        for (long x = 0; x < W; x++) {
            long i = y * W + x;
            if (!intra[i]) continue;
            int64_t a = (x > 0) ? rec[i - 1] : ((y > 0) ? rec[i - W] : 128);
            int64_t b = (y > 0) ? rec[i - W] : a;
            int64_t c = (x > 0 && y > 0) ? rec[i - W - 1] : b;
            int64_t mx = a > b ? a : b, mn = a < b ? a : b;
            int64_t pred = (c >= mx) ? mn : ((c <= mn) ? mx : a + b - c);
            rec[i] = pred + residual[i];
        }
    }
}

/* --- LZ match-finder forward pass (mirrors tokenizer.tokenize_optimal) ------
 *
 * Builds 3-byte hash chains over the combined buffer and, for each data position
 * p in [base, N), finds the maximal in-file match per distinct length keeping the
 * smallest distance (chains run newest-first, so the first time a length appears
 * its distance is already minimal). Candidates are emitted in first-appearance
 * order, exactly as the Python dict preserves them, so the downstream DP makes
 * identical choices. This is the 60%+ hot loop (the per-position _match_len
 * search); the cost-optimal DP itself stays in Python on these integer-exact
 * candidates, so the produced tokens are byte-identical to the pure-Python parse.
 *
 * The 3-byte key (MIN_MATCH==3) indexes a direct 2^24 table — exact, no hash
 * collisions, so chains contain only true 3-byte matches like the Python dict.
 *
 * CSR output: out_off[N-base+1] gives each position's slice into out_len/out_dist.
 * Returns total candidates, -1 if the buffers are too small, -2 if min_match!=3.
 */
#define HEAD_BITS 24
#define HEAD_SIZE (1 << HEAD_BITS)

long lz_forward(const uint8_t *c, long N, long base, long window,
                int max_match, int max_chain, int min_match,
                int *out_off, int *out_len, int *out_dist, long cap) {
    if (min_match != 3) return -2;
    int32_t *head = (int32_t *)malloc((size_t)HEAD_SIZE * sizeof(int32_t));
    int32_t *prev = (int32_t *)malloc((size_t)N * sizeof(int32_t));
    if (!head || !prev) { free(head); free(prev); return -1; }
    memset(head, 0xFF, (size_t)HEAD_SIZE * sizeof(int32_t));   /* all -1 */

    #define INSERT(i) do {                                              \
        if ((i) + 3 <= N) {                                             \
            uint32_t _k = ((uint32_t)c[i] << 16) | ((uint32_t)c[(i)+1] << 8) | c[(i)+2]; \
            prev[i] = head[_k]; head[_k] = (int32_t)(i);                \
        } else prev[i] = -1;                                            \
    } while (0)

    /* scratch for the per-position found set (distinct length count <= max_chain) */
    int fcap = max_match + 1;
    int *flen = (int *)malloc((size_t)fcap * sizeof(int));
    int *fdist = (int *)malloc((size_t)fcap * sizeof(int));
    if (!flen || !fdist) { free(head); free(prev); free(flen); free(fdist); return -1; }

    for (long i = 0; i < base; i++) INSERT(i);

    long total = 0;
    for (long p = base; p < N; p++) {
        out_off[p - base] = (int)total;
        int fc = 0;
        if (p + 3 <= N) {
            uint32_t key = ((uint32_t)c[p] << 16) | ((uint32_t)c[p+1] << 8) | c[p+2];
            long cand = head[key];
            int chain = max_chain;
            long limit = (max_match < N - p) ? max_match : (N - p);
            while (cand != -1 && p - cand <= window && chain > 0) {
                long n = 0;
                while (n < limit && c[cand + n] == c[p + n]) n++;
                if (n >= min_match) {
                    int seen = 0;
                    for (int j = 0; j < fc; j++) if (flen[j] == (int)n) { seen = 1; break; }
                    if (!seen && fc < fcap) { flen[fc] = (int)n; fdist[fc] = (int)(p - cand); fc++; }
                }
                cand = prev[cand];
                chain--;
            }
        }
        if (total + fc > cap) { free(head); free(prev); free(flen); free(fdist); return -1; }
        for (int j = 0; j < fc; j++) { out_len[total] = flen[j]; out_dist[total] = fdist[j]; total++; }
        INSERT(p);
    }
    out_off[N - base] = (int)total;
    free(head); free(prev); free(flen); free(fdist);
    #undef INSERT
    return total;
}

/* Greedy single-best match per position (mirrors tokenizer._find_lz): longest
 * match, smallest distance on ties (chains newest-first). best_len/best_dist are
 * 0 where nothing qualifies. Used by the greedy/lazy parse (training). Integer-
 * exact, so the produced tokens are identical to the Python parse. Returns -2 if
 * min_match != 3, 0 on success, -1 on allocation failure. */
long lz_best(const uint8_t *c, long N, long base, long window,
             int max_match, int max_chain, int min_match,
             int *best_len, int *best_dist) {
    if (min_match != 3) return -2;
    int32_t *head = (int32_t *)malloc((size_t)HEAD_SIZE * sizeof(int32_t));
    int32_t *prev = (int32_t *)malloc((size_t)N * sizeof(int32_t));
    if (!head || !prev) { free(head); free(prev); return -1; }
    memset(head, 0xFF, (size_t)HEAD_SIZE * sizeof(int32_t));

    #define INSERT(i) do {                                              \
        if ((i) + 3 <= N) {                                             \
            uint32_t _k = ((uint32_t)c[i] << 16) | ((uint32_t)c[(i)+1] << 8) | c[(i)+2]; \
            prev[i] = head[_k]; head[_k] = (int32_t)(i);                \
        } else prev[i] = -1;                                            \
    } while (0)

    for (long i = 0; i < base; i++) INSERT(i);
    for (long p = base; p < N; p++) {
        int bl = 0, bd = 0;
        if (p + 3 <= N) {
            uint32_t key = ((uint32_t)c[p] << 16) | ((uint32_t)c[p+1] << 8) | c[p+2];
            long cand = head[key];
            int chain = max_chain;
            long limit = (max_match < N - p) ? max_match : (N - p);
            while (cand != -1 && p - cand <= window && chain > 0) {
                long n = 0;
                while (n < limit && c[cand + n] == c[p + n]) n++;
                if ((int)n > bl) { bl = (int)n; bd = (int)(p - cand); if (n == limit) break; }
                cand = prev[cand];
                chain--;
            }
        }
        best_len[p - base] = bl; best_dist[p - base] = bd;
        INSERT(p);
    }
    free(head); free(prev);
    #undef INSERT
    return 0;
}

/* --- cost-optimal backward DP (mirrors tokenizer.tokenize_optimal's DP) ------
 *
 * Given the per-position LZ candidates (CSR off/clen/cdist), the per-position
 * dict match (dpid/dlen), and cost tables built from the model, compute the
 * minimum-cost parse and walk it into tokens. All arithmetic is on the exact
 * double cost values supplied (lit_table[byte], dict_table[pid], and
 * mc_table[lslot*ND+dslot] — match cost depends only on the two slots), in the
 * same order and with the same strict-< tie-breaking as the Python DP, so the
 * tokens are identical. Returns the token count. Token encoding matches
 * lz_encode's: kind 0 lit (aval=byte), 1 dict (aval=pid), 2 match (aval=length,
 * bval=distance). */
long lz_dp(const uint8_t *c, long N, long base,
           const int *off, const int *clen, const int *cdist,
           const int *dpid, const int *dlen,
           const double *lit_table, const double *dict_table,
           const double *mc_table, int ND, int min_match,
           int *out_kind, int64_t *out_aval, int64_t *out_bval) {
    double *cte = (double *)malloc((size_t)(N + 1) * sizeof(double));
    int *ck = (int *)malloc((size_t)N * sizeof(int));
    int *ca = (int *)malloc((size_t)N * sizeof(int));
    int *cb = (int *)malloc((size_t)N * sizeof(int));
    if (!cte || !ck || !ca || !cb) { free(cte); free(ck); free(ca); free(cb); return -1; }
    cte[N] = 0.0;
    for (long p = N - 1; p >= base; p--) {
        long pi = p - base;
        double best = lit_table[c[p]] + cte[p + 1];
        int bk = 0, ba = c[p], bb = 0;
        int dl = dlen[pi];
        if (dl >= min_match) {
            double cc = dict_table[dpid[pi]] + cte[p + dl];
            if (cc < best) { best = cc; bk = 1; ba = dpid[pi]; bb = dl; }
        }
        for (int idx = off[pi]; idx < off[pi + 1]; idx++) {
            int length = clen[idx], dist = cdist[idx];
            int lslot = 63 - __builtin_clzll((uint64_t)(length - min_match + 1));
            int dslot = 63 - __builtin_clzll((uint64_t)dist);
            double cc = mc_table[lslot * ND + dslot] + cte[p + length];
            if (cc < best) { best = cc; bk = 2; ba = length; bb = dist; }
        }
        cte[p] = best; ck[pi] = bk; ca[pi] = ba; cb[pi] = bb;
    }
    long nt = 0;
    for (long p = base; p < N; ) {
        long pi = p - base;
        int k = ck[pi];
        out_kind[nt] = k;
        if (k == 0)      { out_aval[nt] = ca[pi]; out_bval[nt] = 0;      p += 1; }
        else if (k == 1) { out_aval[nt] = ca[pi]; out_bval[nt] = 0;      p += cb[pi]; }
        else             { out_aval[nt] = ca[pi]; out_bval[nt] = cb[pi]; p += ca[pi]; }
        nt++;
    }
    free(cte); free(ck); free(ca); free(cb);
    return nt;
}

/* --- trained-dictionary longest-match per position (mirrors Dictionary.match) -
 *
 * For each position p in [base, N), the longest dictionary pattern that is a
 * prefix of c[p:]. Patterns are passed flat (pat_data + pat_off) with a 2-byte
 * prefix index (bucket_off CSR over bucket_pids, pattern ids ordered longest-
 * first within each key) — exactly the Python index. out_pid/out_len get the
 * match (pid, length) or (-1, 0). Replaces the per-position dictionary.match
 * Python call in every parse path. */
void dict_match_all(const uint8_t *c, long N, long base, int min_match,
                    const uint8_t *pat_data, const int *pat_off, int npat,
                    const int *bucket_off, const int *bucket_pids,
                    int *out_pid, int *out_len) {
    (void)npat;
    for (long p = base; p < N; p++) {
        long pi = p - base;
        out_pid[pi] = -1; out_len[pi] = 0;
        if (p + 2 > N) continue;
        int key = ((int)c[p] << 8) | c[p + 1];
        for (int idx = bucket_off[key]; idx < bucket_off[key + 1]; idx++) {
            int pid = bucket_pids[idx];
            int plen = pat_off[pid + 1] - pat_off[pid];
            if (plen < min_match) continue;       /* bucket is longest-first */
            if (p + plen > N) continue;
            if (memcmp(c + p, pat_data + pat_off[pid], (size_t)plen) == 0) {
                out_pid[pi] = pid; out_len[pi] = plen;
                break;
            }
        }
    }
}
