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
