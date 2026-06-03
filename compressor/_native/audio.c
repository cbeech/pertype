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

static void ctx_init(int freq[CTX_NB][CTX_NB], long tot[CTX_NB]) {
    for (int c = 0; c < CTX_NB; c++) {
        for (int s = 0; s < CTX_NB; s++) freq[c][s] = 1;
        tot[c] = CTX_NB;
    }
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
    int freq[CTX_NB][CTX_NB]; long tot[CTX_NB];
    ctx_init(freq, tot);
    bitw w = { out, cap, 0, 0, 0, 0 };
    aenc e = { 0, AC_MAX, 0, &w };
    int ctx = 0;
    for (long i = 0; i < n; i++) {
        int64_t r = res[i];
        uint64_t u = (((uint64_t)r) << 1) ^ (uint64_t)(r >> 63);   /* zigzag */
        int k = u ? (64 - __builtin_clzll(u)) : 0;
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
        ctx = k;
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
    int freq[CTX_NB][CTX_NB]; long tot[CTX_NB];
    ctx_init(freq, tot);
    adec d = { 0, AC_MAX, 0, in, len, 0 };
    for (int i = 0; i < 32; i++) d.code = (d.code << 1) | (uint64_t)ad_bit(&d);
    int ctx = 0;
    for (long i = 0; i < n; i++) {
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
        ctx = k;
    }
}
