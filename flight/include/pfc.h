/* pfc.h — pertype-flight: lossless flight compression core (public API).
 *
 * Asymmetric codec: the ENCODER runs on a spacecraft (no dynamic memory, integer-only,
 * deterministic), the DECODER runs on the ground. The compressed stream is endianness-neutral
 * (canonical little-endian on the wire) so a big-endian flight CPU and a little-endian ground
 * station interoperate byte-for-byte.
 *
 * Memory model (R2/R3): the library never calls malloc. All working memory is a caller-supplied
 * `pfc_ctx` whose size is the compile-time constant PFC_WORKMEM_BYTES — allocate one statically.
 *
 * SPDX-License-Identifier: Apache-2.0
 */
#ifndef PFC_H
#define PFC_H

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define PFC_VERSION 1u

/* ---- compile-time limits (override with -D to retune flight memory) -------------------- */
#ifndef PFC_MAX_COLS
#define PFC_MAX_COLS 8192u        /* max image width (R3) */
#endif
#ifndef PFC_BAND_ROWS
#define PFC_BAND_ROWS 16u         /* rows per independently-decodable block (R6) */
#endif

#define PFC_KMAX  32u             /* max residual magnitude category (covers 32-bit residuals) */
#define PFC_NSYM  (PFC_KMAX + 1u) /* category alphabet 0..KMAX */
#define PFC_NCTX  36u             /* image magnitude ctx (<=18) + run contexts (34,35); seq <=32 */

/* Per-band scratch must hold a worst-case (store-raw) band payload. */
#define PFC_SCRATCH_BYTES ((size_t)PFC_MAX_COLS * PFC_BAND_ROWS * 2u + 16u)

/* ---- status codes --------------------------------------------------------------------- */
typedef enum {
    PFC_OK = 0,
    PFC_E_PARAM,        /* bad arguments / dimensions exceed compile-time limits */
    PFC_E_BOUND,        /* destination buffer too small (see pfc_bound) */
    PFC_E_CORRUPT,      /* stream malformed, or a block failed CRC (data still delivered, repaired) */
    PFC_E_UNSUPPORTED   /* codec id / version not supported by this build */
} pfc_status;

/* ---- codecs (slice 1 implements IMAGE) ------------------------------------------------ */
typedef enum {
    PFC_CODEC_IMAGE    = 1,  /* 2-D MED + bias + run mode + range coder; 8/16-bit gray plane */
    PFC_CODEC_SEQ      = 2,  /* 1-D order-1 delta */
    PFC_CODEC_FLOAT    = 3,  /* float byte-plane split */
    PFC_CODEC_COLUMNAR = 4,  /* record de-interleave */
    PFC_CODEC_SPECTRAL = 5   /* multi/hyperspectral cube: inter-band MED-of-difference prediction */
} pfc_codec;

/* Codec parameters. Only the fields named for the chosen codec are read.
 *  IMAGE:    width*height samples, bitdepth in {8,16}; src is native uint8/uint16.
 *  SEQ:      count integers of elem bytes (1/2/4), is_signed; src is native ints. 1-D delta.
 *  FLOAT:    count floats of elem bytes (4/8); src is raw float bytes. Byte-plane split (lossless).
 *  COLUMNAR: count records of width bytes each (row-major); de-interleave + per-plane delta.
 *  SPECTRAL: count bands, each height*width samples (BSQ), bitdepth in {8,16}; native uint8/uint16.
 *            `elem` is reused here as the INTER-BAND REFRESH INTERVAL (0 = off, the default).
 *            Non-zero means every N'th band is coded spatially-only, giving up inter-band
 *            prediction for that band in exchange for BOUNDING ERROR PROPAGATION: without it, one
 *            corrupt block poisons the prediction reference for every later band, so a one-block
 *            loss becomes a whole-cube loss (measured — see docs/mission-safety.md §2.5). With
 *            N set, damage cannot spread past the next refresh band, at a small compression cost.
 *            0 reproduces the original behaviour byte-for-byte, so existing callers are unaffected;
 *            the value travels in the stream header, so decoding never needs out-of-band config. */
typedef struct {
    uint32_t width;       /* IMAGE/SPECTRAL: pixels/row.  COLUMNAR: bytes/record. */
    uint32_t height;      /* IMAGE/SPECTRAL: rows per band. */
    uint32_t count;       /* SEQ/FLOAT: #elements.  COLUMNAR: #records.  SPECTRAL: #bands. */
    uint8_t  bitdepth;    /* IMAGE/SPECTRAL: 8 or 16. */
    uint8_t  elem;        /* SEQ: 1/2/4.  FLOAT: 4/8. */
    uint8_t  is_signed;   /* SEQ: 1 if samples are signed. */
} pfc_params;

/* ---- working memory (caller-owned, no malloc) ----------------------------------------- */
typedef struct pfc_ctx pfc_ctx;          /* opaque; size == PFC_WORKMEM_BYTES */
size_t pfc_workmem_bytes(void);          /* == sizeof(struct pfc_ctx) */
#define PFC_WORKMEM_BYTES (pfc_workmem_bytes())

/* ---- API ------------------------------------------------------------------------------- */

/* Hard worst-case output size for `n_in` input bytes; pfc_encode never writes more. (R5) */
size_t pfc_bound(pfc_codec codec, size_t n_in);

/* Encode. Returns PFC_OK and sets *out to the byte count written into dst[0..cap).
 * Never expands past pfc_bound(); incompressible bands fall back to store-raw. */
pfc_status pfc_encode(pfc_codec codec, const pfc_params *p,
                      const void *src, size_t src_len,
                      void *dst, size_t cap, size_t *out,
                      pfc_ctx *work);

/* Decode a stream produced by pfc_encode into dst[0..cap). Sets *out to bytes written.
 * Error containment (R6): a block that fails CRC is repaired (filled, not propagated) and the
 * function returns PFC_E_CORRUPT while still delivering a fully-written dst. */
pfc_status pfc_decode(const void *src, size_t src_len,
                      void *dst, size_t cap, size_t *out,
                      pfc_ctx *work);

#ifdef __cplusplus
}
#endif
#endif /* PFC_H */
