/* fastq.c — C twin of pertype/fastqcodec.py (FQS1 whole-file FASTQ codec).
 *
 * Byte-identical to the pure-Python reference: same WNC arithmetic coder
 * (MSB-first bit output, zero-padded final byte), same order-1 byte-context
 * ctxblob model (INCR 32, RESCALE 1<<14, (x+1)>>1 halving), same varint /
 * zigzag framing, same k=12 reverse-complement orientation rule, same 2-bit
 * packing, and the same LZMA2 raw stream (liblzma preset 9).
 *
 * liblzma is reached by dlopen at runtime (no dev headers needed): the three
 * symbols used are resolved from liblzma.so.5 (or .so), with the small
 * ABI-stable structs declared locally. If liblzma is absent the whole codec
 * reports unavailable and the caller falls back to pure Python. On Windows
 * the LZMA path is stubbed out (always unavailable).
 *
 * The quality payload is produced/consumed by the caller (the qualcodec
 * twin, which has its own byte-identical C), so this file does not duplicate
 * the quality model.
 */
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

#ifndef _WIN32
#include <dlfcn.h>
#endif

/* ------------------------------------------------------------ arithmetic -- */
#define AC_MAX   0xFFFFFFFFu
#define AC_HALF  0x80000000u
#define AC_QUARTER 0x40000000u
#define AC_3QUARTER 0xC0000000u

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

static long ae_finish(aenc *e) {                 /* finish() + getvalue() */
    e->pending++;
    ae_emit(e, e->low < AC_QUARTER ? 0 : 1);
    if (e->w->overflow) return -1;
    if (e->w->nbits > 0) {
        if (e->w->byte >= e->w->cap) return -1;
        e->w->out[e->w->byte++] = (uint8_t)(e->w->cur << (8 - e->w->nbits));
    }
    return e->w->byte;
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

static void ad_init(adec *d, const uint8_t *in, long len) {
    d->low = 0; d->high = AC_MAX; d->code = 0; d->in = in; d->len = len; d->pos = 0;
    for (int i = 0; i < 32; i++) d->code = (d->code << 1) | (uint64_t)ad_bit(d);
}

/* ------------------------------------------------------------- ctxblob ---- */
#define FQ_INCR 32
#define FQ_RESCALE (1 << 14)

typedef struct { int cnt[256]; long tot; } fqctx;

/* Encode ``n`` raw bytes with the order-1 byte-context model. Writes
 * varint(n) + coded bytes into out; returns length or -1. */
static long fq_ctx_encode(const uint8_t *data, long n, uint8_t *out, long cap) {
    static fqctx ctx[256];
    memset(ctx, 0, sizeof(ctx));               /* lazy dict equivalent: */
    for (int c = 0; c < 256; c++) {            /* contexts start unvisited;   */
        for (int s = 0; s < 256; s++) ctx[c].cnt[s] = 1;  /* wasteful but simple */
        ctx[c].tot = 256;
    }
    long pos = 0;
    uint64_t m = (uint64_t)n;
    while (m >= 0x80) { if (pos >= cap) return -1; out[pos++] = (uint8_t)(m | 0x80); m >>= 7; }
    if (pos >= cap) return -1; out[pos++] = (uint8_t)m;
    bitw w = { out + pos, cap - pos, 0, 0, 0, 0 };
    aenc e = { 0, AC_MAX, 0, &w };
    int prev = 0;
    for (long i = 0; i < n; i++) {
        int b = data[i];
        fqctx *C = &ctx[prev];
        uint64_t cum = 0;
        for (int s = 0; s < b; s++) cum += (uint64_t)C->cnt[s];
        ae_encode(&e, cum, (uint64_t)C->cnt[b], (uint64_t)C->tot);
        C->cnt[b] += FQ_INCR; C->tot += FQ_INCR;
        if (C->tot >= FQ_RESCALE) {
            long t = 0;
            for (int s = 0; s < 256; s++) { C->cnt[s] = (C->cnt[s] + 1) >> 1; t += C->cnt[s]; }
            C->tot = t;
        }
        prev = b;
    }
    long r = ae_finish(&e);
    if (r < 0) return -1;
    return pos + r;
}

/* Decode a ctxblob (varint rawlen + coded bytes). Returns raw bytes written
 * into out (== rawlen), or -1. */
static long fq_ctx_decode(const uint8_t *in, long len, uint8_t *out, long outcap) {
    long pos = 0;
    uint64_t n = 0;
    int shift = 0;
    for (;;) {
        if (pos >= len) return -1;
        uint8_t b = in[pos++];
        n |= (uint64_t)(b & 0x7F) << shift;
        if (!(b & 0x80)) break;
        shift += 7;
    }
    if ((uint64_t)outcap < n) return -1;
    static fqctx ctx[256];
    memset(ctx, 0, sizeof(ctx));
    for (int c = 0; c < 256; c++) {
        for (int s = 0; s < 256; s++) ctx[c].cnt[s] = 1;
        ctx[c].tot = 256;
    }
    adec d;
    ad_init(&d, in + pos, len - pos);
    int prev = 0;
    for (uint64_t i = 0; i < n; i++) {
        fqctx *C = &ctx[prev];
        uint64_t total = (uint64_t)C->tot;
        uint64_t target = ad_target(&d, total);
        uint64_t cum = 0;
        int s = 0;
        while (s < 255 && cum + (uint64_t)C->cnt[s] <= target) { cum += (uint64_t)C->cnt[s]; s++; }
        ad_update(&d, cum, (uint64_t)C->cnt[s], total);
        out[i] = (uint8_t)s;
        C->cnt[s] += FQ_INCR; C->tot += FQ_INCR;
        if (C->tot >= FQ_RESCALE) {
            long t = 0;
            for (int j = 0; j < 256; j++) { C->cnt[j] = (C->cnt[j] + 1) >> 1; t += C->cnt[j]; }
            C->tot = t;
        }
        prev = s;
    }
    return (long)n;
}

/* Decode a ctxblob into a freshly malloc'd buffer; sets *outn. NULL on error. */
static uint8_t *fq_ctx_decode_alloc(const uint8_t *in, long len, long *outn) {
    long pos = 0;
    uint64_t n = 0;
    int shift = 0;
    for (;;) {
        if (pos >= len) return 0;
        uint8_t b = in[pos++];
        n |= (uint64_t)(b & 0x7F) << shift;
        if (!(b & 0x80)) break;
        shift += 7;
    }
    uint8_t *out = malloc(n ? n : 1);
    if (!out) return 0;
    static fqctx ctx[256];
    memset(ctx, 0, sizeof(ctx));
    for (int c = 0; c < 256; c++) {
        for (int s = 0; s < 256; s++) ctx[c].cnt[s] = 1;
        ctx[c].tot = 256;
    }
    adec d;
    ad_init(&d, in + pos, len - pos);
    int prev = 0;
    for (uint64_t i = 0; i < n; i++) {
        fqctx *C = &ctx[prev];
        uint64_t total = (uint64_t)C->tot;
        uint64_t target = ad_target(&d, total);
        uint64_t cum = 0;
        int s = 0;
        while (s < 255 && cum + (uint64_t)C->cnt[s] <= target) { cum += (uint64_t)C->cnt[s]; s++; }
        ad_update(&d, cum, (uint64_t)C->cnt[s], total);
        out[i] = (uint8_t)s;
        C->cnt[s] += FQ_INCR; C->tot += FQ_INCR;
        if (C->tot >= FQ_RESCALE) {
            long t = 0;
            for (int j = 0; j < 256; j++) { C->cnt[j] = (C->cnt[j] + 1) >> 1; t += C->cnt[j]; }
            C->tot = t;
        }
        prev = s;
    }
    *outn = (long)n;
    return out;
}

/* -------------------------------------------------------------- varints --- */
static void fq_wv(uint8_t *buf, long *pos, uint64_t n) {
    while (n >= 0x80) { buf[(*pos)++] = (uint8_t)(n | 0x80); n >>= 7; }
    buf[(*pos)++] = (uint8_t)n;
}

static int fq_rv(const uint8_t *buf, long len, long *pos, uint64_t *out) {
    uint64_t n = 0;
    int shift = 0;
    for (;;) {
        if (*pos >= len || shift > 63) return -1;
        uint8_t b = buf[(*pos)++];
        n |= (uint64_t)(b & 0x7F) << shift;
        if (!(b & 0x80)) { *out = n; return 0; }
        shift += 7;
    }
}

static uint64_t fq_zz(int64_t v) { return ((uint64_t)v << 1) ^ (uint64_t)(v >> 63); }
static int64_t fq_unzz(uint64_t m) { return (int64_t)(m >> 1) ^ -(int64_t)(m & 1); }

/* -------------------------------------------------------- lzma via dlopen -- */
typedef uint64_t fq_vli;
typedef struct { fq_vli id; void *options; } fq_filter;
#define FQ_LZMA2 0x21

typedef int (*fq_preset_fn)(void *options, uint32_t preset);
typedef int (*fq_raw_enc_fn)(const fq_filter *filters, const void *allocator,
                             const uint8_t *in, size_t in_size,
                             uint8_t *out, size_t *out_pos, size_t out_size);
typedef int (*fq_raw_dec_fn)(const fq_filter *filters, const void *allocator,
                             const uint8_t *in, size_t *in_pos, size_t in_size,
                             uint8_t *out, size_t *out_pos, size_t out_size);

static void *fq_lzma_handle = 0;
static fq_preset_fn fq_preset = 0;
static fq_raw_enc_fn fq_raw_enc = 0;
static fq_raw_dec_fn fq_raw_dec = 0;
static int fq_lzma_state = 0;   /* 0 untried, 1 ok, -1 unavailable */

static int fq_lzma_init(void) {
    if (fq_lzma_state) return fq_lzma_state;
#ifdef _WIN32
    fq_lzma_state = -1;
#else
    void *h = dlopen("liblzma.so.5", RTLD_LAZY);
    if (!h) h = dlopen("liblzma.so", RTLD_LAZY);
    if (!h) { fq_lzma_state = -1; return -1; }
    fq_preset = (fq_preset_fn)dlsym(h, "lzma_lzma_preset");
    fq_raw_enc = (fq_raw_enc_fn)dlsym(h, "lzma_raw_buffer_encode");
    fq_raw_dec = (fq_raw_dec_fn)dlsym(h, "lzma_raw_buffer_decode");
    if (!fq_preset || !fq_raw_enc || !fq_raw_dec) { fq_lzma_state = -1; return -1; }
    fq_lzma_handle = h;
    fq_lzma_state = 1;
#endif
    return fq_lzma_state;
}

long fastq_native_available(void) { return fq_lzma_init() == 1 ? 1 : 0; }

/* lzma_options_lzma is ABI-stable but version-dependent in size; allocate a
 * generously oversized zeroed buffer and let lzma_lzma_preset fill the start. */
static int fq_lzma_compress(const uint8_t *in, long n, uint8_t *out, long cap,
                            long *outlen) {
    if (fq_lzma_init() != 1) return -1;
    uint8_t opts[512];
    memset(opts, 0, sizeof(opts));
    if (fq_preset(opts, 9) != 0) return -1;
    fq_filter filters[2] = { { FQ_LZMA2, opts }, { ~(fq_vli)0, 0 } };
    size_t op = 0;
    int rc = fq_raw_enc(filters, 0, in, (size_t)n, out, &op, (size_t)cap);
    if (rc != 0) return -1;
    *outlen = (long)op;
    return 0;
}

static int fq_lzma_decompress(const uint8_t *in, long n, uint8_t *out, long cap) {
    if (fq_lzma_init() != 1) return -1;
    uint8_t opts[512];
    memset(opts, 0, sizeof(opts));
    if (fq_preset(opts, 9) != 0) return -1;
    fq_filter filters[2] = { { FQ_LZMA2, opts }, { ~(fq_vli)0, 0 } };
    size_t ip = 0, op = 0;
    int rc = fq_raw_dec(filters, 0, in, &ip, (size_t)n, out, &op, (size_t)cap);
    if (rc != 0 || op != (size_t)cap) return -1;
    return 0;
}

/* --------------------------------------------------------- header model --- */
#define FQ_MAX_RUNS 64

typedef struct {
    int flags[FQ_MAX_RUNS];      /* 1 = integer run */
    const uint8_t *txt[FQ_MAX_RUNS]; long tlen[FQ_MAX_RUNS];
    int64_t ivals[FQ_MAX_RUNS];  long iw[FQ_MAX_RUNS];  /* raw width of int runs */
    int nruns, ncols;
} fq_hparse;

/* Parse one header into alternating text/integer runs. Returns -1 on too
 * many runs or an integer run wider than 18 digits (int64 safety margin —
 * the Python reference handles those; C declines and the wrapper falls back). */
static int fq_parse_header(const uint8_t *h, long n, fq_hparse *p) {
    long i = 0;
    p->nruns = 0; p->ncols = 0;
    while (i < n) {
        if (p->nruns >= FQ_MAX_RUNS) return -1;
        long j = i;
        if (h[i] >= '0' && h[i] <= '9') {
            while (j < n && h[j] >= '0' && h[j] <= '9') j++;
            if (j - i > 18) return -1;
            int64_t v = 0;
            for (long k = i; k < j; k++) v = v * 10 + (h[k] - '0');
            p->flags[p->nruns] = 1;
            p->ivals[p->ncols] = v;
            p->iw[p->ncols] = j - i;
            p->ncols++;
        } else {
            while (j < n && !(h[j] >= '0' && h[j] <= '9')) j++;
            p->flags[p->nruns] = 0;
            p->txt[p->nruns] = h + i;
            p->tlen[p->nruns] = j - i;
        }
        p->nruns++;
        i = j;
    }
    return 0;
}

static int fq_padint(int64_t v, long width, char *tmp) {
    int len = snprintf(tmp, 32, "%lld", (long long)v);
    if (len >= width) return len;
    int pad = (int)width - len;
    memmove(tmp + pad, tmp, (size_t)len + 1);
    memset(tmp, '0', (size_t)pad);
    return (int)width;
}
/* ------------------------------------------------------------- encode ----- */
#define FQK 12
#define FQ_KMASK ((1u << (2 * FQK)) - 1)

static inline int fq_b2i(uint8_t c) {
    switch (c) {
        case 'A': return 0;
        case 'C': return 1;
        case 'G': return 2;
        case 'T': return 3;
        default: return -1;
    }
}

typedef struct {
    const uint8_t *p;
    long n;
} fq_line;

long fastq_encode(const uint8_t *data, long dlen, const uint8_t *qp, long qplen,
                  uint8_t *out, long cap) {
    if (fq_lzma_init() != 1) return -1;
    if (dlen <= 0) return -1;

    /* --- split lines (final empty piece after a trailing newline is not
     * appended, mirroring Python's parts[:-1] when parts[-1] == "") --- */
    long nlines_alloc = dlen / 2 + 2;
    fq_line *lines = malloc(sizeof(fq_line) * (size_t)nlines_alloc);
    if (!lines) return -1;
    long nlines = 0;
    long start = 0;
    for (long i = 0; i < dlen; i++) {
        if (data[i] == '\n') {
            if (nlines >= nlines_alloc) goto oom0;
            lines[nlines].p = data + start;
            lines[nlines].n = i - start;
            nlines++;
            start = i + 1;
        }
    }
    int trailing;
    if (start == dlen) {
        trailing = 1;
    } else {
        if (nlines >= nlines_alloc) goto oom0;
        lines[nlines].p = data + start;
        lines[nlines].n = dlen - start;
        nlines++;
        trailing = 0;
    }
    if (nlines == 0 || nlines % 4 != 0) goto oom0;
    long nrec = nlines / 4;

    for (long r = 0; r < nrec; r++) {
        fq_line *sq = &lines[4 * r + 1], *ql = &lines[4 * r + 3];
        if (sq->n != ql->n) goto oom0;
        for (long j = 0; j < ql->n; j++)
            if (ql->p[j] < 33 || ql->p[j] > 126) goto oom0;
    }

    /* --- header template from the first header --- */
    fq_hparse hp0;
    if (fq_parse_header(lines[0].p, lines[0].n, &hp0) != 0) goto oom0;
    int ncols = hp0.ncols;

    uint8_t *tmpl = malloc(1024);
    uint8_t *colraw = malloc((size_t)ncols * ((size_t)(nrec + 1) * 12 + 32) + 64);
    uint8_t *exc = malloc((size_t)dlen / 2 + 4096);
    uint8_t *lraw = malloc((size_t)nrec * 12 + 64);
    uint8_t *bmp = malloc((size_t)nrec / 8 + 16);
    uint8_t *npraw = malloc((size_t)dlen / 2 + 64);
    uint8_t *packed = malloc((size_t)dlen / 4 + 16);
    uint8_t *lz = malloc((size_t)dlen / 2 + 65536);
    long *colpos = calloc((size_t)ncols, sizeof(long));
    uint8_t *praw = 0;
    if (!tmpl || !colraw || !exc || !lraw || !bmp || !npraw || !packed || !lz || !colpos)
        goto oom;

    long colstride = (nrec + 1) * 12 + 32;
    long tpos = 0;
    fq_wv(tmpl, &tpos, (uint64_t)hp0.nruns);
    {
        int ci = 0;
        for (int r = 0; r < hp0.nruns; r++) {
            tmpl[tpos++] = (uint8_t)hp0.flags[r];
            if (!hp0.flags[r]) {
                fq_wv(tmpl, &tpos, (uint64_t)hp0.tlen[r]);
                memcpy(tmpl + tpos, hp0.txt[r], (size_t)hp0.tlen[r]);
                tpos += hp0.tlen[r];
            } else {
                fq_wv(tmpl, &tpos, (uint64_t)hp0.iw[ci]);
                ci++;
            }
        }
    }

    for (int c = 0; c < ncols; c++)
        fq_wv(colraw + (size_t)c * colstride, &colpos[c], (uint64_t)hp0.ivals[c]);
    int64_t prev[FQ_MAX_RUNS];
    for (int c = 0; c < ncols; c++) prev[c] = hp0.ivals[c];
    long epos = 0;
    long nexc = 0, last_idx = 0;
    fq_hparse hp;
    for (long idx = 1; idx < nrec; idx++) {
        const uint8_t *h = lines[4 * idx].p;
        long hn = lines[4 * idx].n;
        int bad = fq_parse_header(h, hn, &hp) != 0;
        if (!bad && (hp.nruns != hp0.nruns || hp.ncols != ncols)) bad = 1;
        if (!bad) {
            for (int r = 0; r < hp.nruns && !bad; r++) {
                if (hp.flags[r] != hp0.flags[r]) { bad = 1; break; }
                if (!hp.flags[r] &&
                    (hp.tlen[r] != hp0.tlen[r] ||
                     memcmp(hp.txt[r], hp0.txt[r], (size_t)hp.tlen[r]) != 0))
                    bad = 1;
            }
        }
        if (!bad) {
            /* each integer run must equal its zero-padded form */
            long pos2 = 0;
            int c2 = 0;
            while (pos2 < hn && !bad) {
                long j = pos2;
                if (h[pos2] >= '0' && h[pos2] <= '9') {
                    while (j < hn && h[j] >= '0' && h[j] <= '9') j++;
                    char tmp[40];
                    int L = fq_padint(hp.ivals[c2], hp0.iw[c2], tmp);
                    if (j - pos2 != L || memcmp(h + pos2, tmp, (size_t)L) != 0) bad = 1;
                    c2++;
                } else {
                    while (j < hn && !(h[j] >= '0' && h[j] <= '9')) j++;
                }
                pos2 = j;
            }
        }
        if (bad) {
            nexc++;
            fq_wv(exc, &epos, (uint64_t)(idx - last_idx));
            last_idx = idx;
            fq_wv(exc, &epos, (uint64_t)hn);
            if (epos + hn > (long)((size_t)dlen / 2 + 4096)) goto oom;
            memcpy(exc + epos, h, (size_t)hn);
            epos += hn;
            continue;
        }
        for (int c = 0; c < ncols; c++)
            fq_wv(colraw + (size_t)c * colstride, &colpos[c],
                  fq_zz(hp.ivals[c] - prev[c]));
        for (int c = 0; c < ncols; c++) prev[c] = hp.ivals[c];
    }

    /* --- lengths varint stream --- */
    long lpos = 0;
    for (long r = 0; r < nrec; r++) fq_wv(lraw, &lpos, (uint64_t)lines[4 * r + 3].n);

    /* --- plus lines --- */
    int pflag = 0;
    for (long r = 0; r < nrec; r++)
        if (lines[4 * r + 2].n != 1 || lines[4 * r + 2].p[0] != '+') { pflag = 1; break; }
    long ppos = 0;
    if (pflag) {
        praw = malloc((size_t)dlen / 2 + 4096);
        if (!praw) goto oom;
        for (long r = 0; r < nrec; r++) {
            fq_wv(praw, &ppos, (uint64_t)lines[4 * r + 2].n);
            memcpy(praw + ppos, lines[4 * r + 2].p, (size_t)lines[4 * r + 2].n);
            ppos += lines[4 * r + 2].n;
        }
    }

    /* --- orientation + 2-bit pack (k=12, ACGT-only k-mers, flip iff RC has
     * strictly more seen hits; bitmap MSB-first) --- */
    uint8_t *seen = calloc(1u << (2 * FQK), 1);
    if (!seen) goto oom;
    long bpos = 0;
    unsigned cur = 0;
    int nbb = 0;
    long pkpos = 0;
    unsigned pcur = 0;
    int pnb = 0;
    long nbase = 0;
    long nppos = 0;
    long nprev = 0;
    for (long r = 0; r < nrec; r++) {
        const uint8_t *s = lines[4 * r + 1].p;
        long L = lines[4 * r + 1].n;
        uint64_t kmer = 0;
        int run = 0;
        long hf = 0, hr = 0;
        for (long j = 0; j < L; j++) {
            int v = fq_b2i(s[j]);
            if (v < 0) { kmer = 0; run = 0; continue; }
            kmer = ((kmer << 2) | (uint64_t)v) & FQ_KMASK;
            if (++run >= FQK) hf += seen[kmer];
        }
        kmer = 0; run = 0;
        for (long j = L - 1; j >= 0; j--) {
            int v = fq_b2i(s[j]);
            if (v < 0) { kmer = 0; run = 0; continue; }
            kmer = ((kmer << 2) | (uint64_t)(3 - v)) & FQ_KMASK;
            if (++run >= FQK) hr += seen[kmer];
        }
        int flip = hr > hf;
        cur = (cur << 1) | (unsigned)flip;
        if (++nbb == 8) { bmp[bpos++] = (uint8_t)cur; cur = 0; nbb = 0; }
        kmer = 0; run = 0;
        for (long jj = 0; jj < L; jj++) {
            long j = flip ? (L - 1 - jj) : jj;
            int v = fq_b2i(s[j]);
            if (v >= 0 && flip) v = 3 - v;
            if (v < 0) {
                fq_wv(npraw, &nppos, (uint64_t)(nbase - nprev));
                nprev = nbase;
                v = 0;
                kmer = 0; run = 0;
            } else {
                kmer = ((kmer << 2) | (uint64_t)v) & FQ_KMASK;
                run++;
            }
            pcur = (pcur << 2) | (unsigned)v;
            nbase++;
            if (++pnb == 4) { packed[pkpos++] = (uint8_t)pcur; pcur = 0; pnb = 0; }
            if (v >= 0 && run >= FQK) seen[kmer] = 1;
        }
    }
    if (nbb) bmp[bpos++] = (uint8_t)(cur << (8 - nbb));
    if (pnb) packed[pkpos++] = (uint8_t)(pcur << (2 * (4 - pnb)));
    free(seen);

    long lzlen = 0;
    if (fq_lzma_compress(packed, pkpos, lz, (long)((size_t)dlen / 2 + 65536), &lzlen) != 0)
        goto oom;

    /* --- assemble --- */
    {
        long pos = 0;
        if (cap < 13) goto oom;
        memcpy(out, "FQS1", 4);
        pos = 4;
        for (int i = 7; i >= 0; i--) out[pos++] = (uint8_t)((uint64_t)nrec >> (8 * i));
        out[pos++] = (uint8_t)trailing;
        const uint8_t *secraw[1 + FQ_MAX_RUNS + 3];
        long secn[1 + FQ_MAX_RUNS + 3];
        secraw[0] = tmpl; secn[0] = tpos;
        for (int c = 0; c < ncols; c++) {
            secraw[1 + c] = colraw + (size_t)c * colstride;
            secn[1 + c] = colpos[c];
        }
        secraw[1 + ncols] = lraw; secn[1 + ncols] = lpos;
        secraw[2 + ncols] = bmp; secn[2 + ncols] = bpos;
        secraw[3 + ncols] = npraw; secn[3 + ncols] = nppos;
        for (int s2 = 0; s2 < 1 + ncols + 3; s2++) {
            long ccap = secn[s2] * 2 + 4096;
            uint8_t *coded = malloc((size_t)ccap);
            if (!coded) goto oom;
            long cn = fq_ctx_encode(secraw[s2], secn[s2], coded, ccap);
            if (cn < 0) { free(coded); goto oom; }
            fq_wv(out, &pos, (uint64_t)cn);
            if (pos + cn > cap) { free(coded); goto oom; }
            memcpy(out + pos, coded, (size_t)cn);
            pos += cn;
            free(coded);
        }
        fq_wv(out, &pos, (uint64_t)nexc);
        if (pos + epos > cap) goto oom;
        memcpy(out + pos, exc, (size_t)epos);
        pos += epos;
        if (pos >= cap) goto oom;
        out[pos++] = (uint8_t)pflag;
        if (pflag) {
            long ccap = ppos * 2 + 4096;
            uint8_t *coded = malloc((size_t)ccap);
            if (!coded) goto oom;
            long cn = fq_ctx_encode(praw, ppos, coded, ccap);
            if (cn < 0) { free(coded); goto oom; }
            fq_wv(out, &pos, (uint64_t)cn);
            if (pos + cn > cap) { free(coded); goto oom; }
            memcpy(out + pos, coded, (size_t)cn);
            pos += cn;
            free(coded);
        }
        if (pos + 8 > cap) goto oom;
        for (int i = 7; i >= 0; i--) out[pos++] = (uint8_t)((uint64_t)nbase >> (8 * i));
        fq_wv(out, &pos, (uint64_t)lzlen);
        if (pos + lzlen > cap) goto oom;
        memcpy(out + pos, lz, (size_t)lzlen);
        pos += lzlen;
        fq_wv(out, &pos, (uint64_t)qplen);
        if (pos + qplen > cap) goto oom;
        memcpy(out + pos, qp, (size_t)qplen);
        pos += qplen;

        free(lines); free(tmpl); free(colraw); free(exc); free(lraw);
        free(bmp); free(npraw); free(packed); free(lz); free(colpos); free(praw);
        return pos;
    }

oom:
    free(lines); free(tmpl); free(colraw); free(exc); free(lraw);
    free(bmp); free(npraw); free(packed); free(lz); free(colpos); free(praw);
    return -1;
oom0:
    free(lines);
    return -1;
}

/* ------------------------------------------------------------- decode ----- */
long fastq_decode(const uint8_t *blob, long blen, const uint8_t *qflat, long qflen,
                  uint8_t *out, long outcap) {
    if (fq_lzma_init() != 1) return -1;
    if (blen < 13 || memcmp(blob, "FQS1", 4) != 0) return -1;
    uint64_t nrec64 = 0;
    for (int i = 0; i < 8; i++) nrec64 = (nrec64 << 8) | blob[4 + i];
    long nrec = (long)nrec64;
    int trailing = blob[12];
    long pos = 13;
    uint64_t L;

    /* --- template --- */
    if (fq_rv(blob, blen, &pos, &L) != 0 || pos + (long)L > blen) return -1;
    const uint8_t *tmpl = blob + pos;
    long tn = (long)L;
    pos += L;
    long tp = 0;
    uint64_t nruns64;
    if (fq_rv(tmpl, tn, &tp, &nruns64) != 0 || nruns64 > FQ_MAX_RUNS) return -1;
    int nruns = (int)nruns64;
    int flags[FQ_MAX_RUNS];
    const uint8_t *txt[FQ_MAX_RUNS];
    long tlen[FQ_MAX_RUNS], iw[FQ_MAX_RUNS];
    int ncols = 0;
    for (int r = 0; r < nruns; r++) {
        if (tp >= tn) return -1;
        flags[r] = tmpl[tp++];
        if (!flags[r]) {
            uint64_t tl;
            if (fq_rv(tmpl, tn, &tp, &tl) != 0 || tp + (long)tl > tn) return -1;
            txt[r] = tmpl + tp;
            tlen[r] = (long)tl;
            tp += tl;
        } else {
            uint64_t w;
            if (fq_rv(tmpl, tn, &tp, &w) != 0) return -1;
            iw[ncols++] = (long)w;
        }
    }

    int64_t **colvals = calloc((size_t)(ncols ? ncols : 1), sizeof(int64_t *));
    long *coln = calloc((size_t)(ncols ? ncols : 1), sizeof(long));
    long *lengths = malloc((size_t)nrec * sizeof(long));
    long *npos = 0;
    long nnpos = 0;
    uint8_t *bmp = 0, *packed = 0, *praw = 0;
    long *exc_idx = 0, *exc_hn = 0;
    const uint8_t **exc_h = 0;
    long *plusn = 0;
    const uint8_t **plusp = 0;
    if (!colvals || !coln || !lengths) goto doom;

    /* --- per-column value streams --- */
    for (int c = 0; c < ncols; c++) {
        colvals[c] = malloc((size_t)(nrec + 1) * sizeof(int64_t));
        if (!colvals[c]) goto doom;
        if (fq_rv(blob, blen, &pos, &L) != 0 || pos + (long)L > blen) goto doom;
        uint8_t *raw = malloc((size_t)(nrec + 1) * 12 + 64);
        if (!raw) goto doom;
        long rn = fq_ctx_decode(blob + pos, L, raw, (nrec + 1) * 12 + 64);
        pos += L;
        if (rn < 0) { free(raw); goto doom; }
        long rp = 0;
        uint64_t v;
        if (fq_rv(raw, rn, &rp, &v) != 0) { free(raw); goto doom; }
        colvals[c][0] = (int64_t)v;
        long nv = 1;
        while (rp < rn) {
            uint64_t m;
            if (fq_rv(raw, rn, &rp, &m) != 0) { free(raw); goto doom; }
            colvals[c][nv] = colvals[c][nv - 1] + fq_unzz(m);
            nv++;
        }
        coln[c] = nv;
        free(raw);
    }

    /* --- lengths ctxblob --- */
    {
        if (fq_rv(blob, blen, &pos, &L) != 0 || pos + (long)L > blen) goto doom;
        uint8_t *raw = malloc((size_t)nrec * 12 + 64);
        if (!raw) goto doom;
        long rn = fq_ctx_decode(blob + pos, L, raw, nrec * 12 + 64);
        pos += L;
        if (rn < 0) { free(raw); goto doom; }
        long rp = 0;
        for (long i = 0; i < nrec; i++) {
            uint64_t v;
            if (fq_rv(raw, rn, &rp, &v) != 0) { free(raw); goto doom; }
            lengths[i] = (long)v;
        }
        free(raw);
    }

    /* --- orientation bitmap ctxblob --- */
    if (fq_rv(blob, blen, &pos, &L) != 0 || pos + (long)L > blen) goto doom;
    bmp = malloc((size_t)nrec / 8 + 16);
    if (!bmp) goto doom;
    if (fq_ctx_decode(blob + pos, L, bmp, nrec / 8 + 16) < 0) goto doom;
    pos += L;

    /* --- N positions ctxblob --- */
    if (fq_rv(blob, blen, &pos, &L) != 0 || pos + (long)L > blen) goto doom;
    {
        long rn = 0;
        uint8_t *raw = fq_ctx_decode_alloc(blob + pos, L, &rn);
        pos += L;
        if (!raw) goto doom;
        npos = malloc(((size_t)rn + 1) * sizeof(long));
        if (!npos) { free(raw); goto doom; }
        long rp = 0;
        long prev = 0;
        while (rp < rn) {
            uint64_t d;
            if (fq_rv(raw, rn, &rp, &d) != 0) { free(raw); goto doom; }
            prev += (long)d;
            npos[nnpos++] = prev;
        }
        free(raw);
    }

    /* --- exceptions --- */
    uint64_t nexc64;
    if (fq_rv(blob, blen, &pos, &nexc64) != 0) goto doom;
    exc_idx = malloc(((size_t)nexc64 + 1) * sizeof(long));
    exc_hn = malloc(((size_t)nexc64 + 1) * sizeof(long));
    exc_h = malloc(((size_t)nexc64 + 1) * sizeof(uint8_t *));
    if (!exc_idx || !exc_hn || !exc_h) goto doom;
    {
        long last = 0;
        for (long e = 0; e < (long)nexc64; e++) {
            uint64_t d, hl;
            if (fq_rv(blob, blen, &pos, &d) != 0) goto doom;
            last += (long)d;
            exc_idx[e] = last;
            if (fq_rv(blob, blen, &pos, &hl) != 0 || pos + (long)hl > blen) goto doom;
            exc_h[e] = blob + pos;
            exc_hn[e] = (long)hl;
            pos += hl;
        }
    }

    /* --- plus --- */
    if (pos >= blen) goto doom;
    int pflag = blob[pos++];
    if (pflag) {
        if (fq_rv(blob, blen, &pos, &L) != 0 || pos + (long)L > blen) goto doom;
        long rn = 0;
        praw = fq_ctx_decode_alloc(blob + pos, L, &rn);
        pos += L;
        if (!praw) goto doom;
        plusn = malloc((size_t)nrec * sizeof(long));
        plusp = malloc((size_t)nrec * sizeof(uint8_t *));
        if (!plusn || !plusp) goto doom;
        long rp = 0;
        for (long i = 0; i < nrec; i++) {
            uint64_t v;
            if (fq_rv(praw, rn, &rp, &v) != 0) goto doom;
            plusp[i] = praw + rp;
            plusn[i] = (long)v;
            rp += (long)v;
        }
    }

    /* --- packed sequences --- */
    uint64_t nbase = 0;
    if (pos + 8 > blen) goto doom;
    for (int i = 0; i < 8; i++) nbase = (nbase << 8) | blob[pos + i];
    pos += 8;
    if (fq_rv(blob, blen, &pos, &L) != 0 || pos + (long)L > blen) goto doom;
    long pkcap = (long)((nbase + 3) / 4);
    packed = malloc((size_t)pkcap + 1);
    if (!packed) goto doom;
    if (fq_lzma_decompress(blob + pos, L, packed, pkcap) != 0) goto doom;
    pos += L;
    /* quality payload section (decoded by the caller into qflat) */
    if (fq_rv(blob, blen, &pos, &L) != 0 || pos + (long)L > blen) goto doom;
    pos += L;
    if (pos != blen) goto doom;

    /* --- rebuild the original FASTQ bytes --- */
    {
        long op = 0, qi = 0, npi = 0, seqoff = 0, e_i = 0, seq_idx = 0;
        for (long i = 0; i < nrec; i++) {
            if (e_i < (long)nexc64 && exc_idx[e_i] == i) {
                if (op + exc_hn[e_i] > outcap) goto doom;
                memcpy(out + op, exc_h[e_i], (size_t)exc_hn[e_i]);
                op += exc_hn[e_i];
                e_i++;
            } else {
                int ci = 0;
                for (int r = 0; r < nruns; r++) {
                    if (!flags[r]) {
                        if (op + tlen[r] > outcap) goto doom;
                        memcpy(out + op, txt[r], (size_t)tlen[r]);
                        op += tlen[r];
                    } else {
                        char tmp[40];
                        int hl = fq_padint(colvals[ci][seq_idx], iw[ci], tmp);
                        if (op + hl > outcap) goto doom;
                        memcpy(out + op, tmp, (size_t)hl);
                        op += hl;
                        ci++;
                    }
                }
                seq_idx++;
            }
            if (op >= outcap) goto doom;
            out[op++] = '\n';
            long Lr = lengths[i];
            int flip = (bmp[i / 8] >> (7 - (i % 8))) & 1;
            if (op + Lr > outcap) goto doom;
            for (long j = 0; j < Lr; j++) {
                long g = seqoff + j;
                uint8_t ch;
                if (npi < nnpos && npos[npi] == g) { ch = 'N'; npi++; }
                else ch = "ACGT"[(packed[g / 4] >> (6 - 2 * (g % 4))) & 3];
                if (flip) {
                    uint8_t cc;
                    switch (ch) {
                        case 'A': cc = 'T'; break;
                        case 'C': cc = 'G'; break;
                        case 'G': cc = 'C'; break;
                        case 'T': cc = 'A'; break;
                        default: cc = ch;
                    }
                    out[op + (Lr - 1 - j)] = cc;
                } else {
                    out[op + j] = ch;
                }
            }
            op += Lr;
            seqoff += Lr;
            if (op >= outcap) goto doom;
            out[op++] = '\n';
            if (pflag) {
                if (op + plusn[i] > outcap) goto doom;
                memcpy(out + op, plusp[i], (size_t)plusn[i]);
                op += plusn[i];
            } else {
                if (op >= outcap) goto doom;
                out[op++] = '+';
            }
            if (op >= outcap) goto doom;
            out[op++] = '\n';
            if (qi + Lr > qflen || op + Lr > outcap) goto doom;
            memcpy(out + op, qflat + qi, (size_t)Lr);
            op += Lr;
            qi += Lr;
            if (i < nrec - 1 || trailing) {
                if (op >= outcap) goto doom;
                out[op++] = '\n';
            }
        }
        for (int c = 0; c < ncols; c++) free(colvals[c]);
        free(colvals); free(coln); free(lengths); free(npos); free(bmp);
        free(packed); free(praw);
        free(exc_idx); free(exc_hn); free(exc_h); free(plusn); free(plusp);
        return op;
    }

doom:
    if (colvals) for (int c = 0; c < ncols; c++) free(colvals[c]);
    free(colvals); free(coln); free(lengths); free(npos); free(bmp);
    free(packed); free(praw);
    free(exc_idx); free(exc_hn); free(exc_h); free(plusn); free(plusp);
    return -1;
}
