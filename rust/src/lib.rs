//! Rust port of the compression codec's hot path — byte-identical to the Python reference
//! (`compressor/`) and its C twin (`compressor/_native/audio.c`), so a blob produced by any
//! of the three decodes in the others. Built as a `cdylib` behind the same C ABI as the C
//! native, a drop-in for the ctypes loader. This is the performance/distribution port; the
//! ratios are validated in Python.
//!
//! Ported so far:
//! * `arith`    — the WNC 32-bit arithmetic coder + bit I/O (shared)
//! * `ctxcoder` — context-adaptive residual coder (every numeric/columnar/float codec)
//! * `calic`    — full CALIC image codec (GAP + bias + energy-conditional coding)
//! * `columnar` — complete standalone codec for fixed-width binary record streams
//! * `imagecodec`/`audiocodec`/`videocodec` — the full image, audio and video codecs
//!   (MED/CALIC/RLE; mid/side + LMS + Rice; motion-compensated inter), byte-identical to
//!   Python/C.
//! * `textcodec` — the trained per-type text/byte codec (transform → cost-optimal LZ+dict
//!   parse → WNC arithmetic coding), byte-identical including the `f64`-priced parse.
//!
//! This is the feature-complete port: every `compressor/` codec has a Rust twin behind the
//! same C ABI (`ctx_encode`/`ctx_decode`, `calic_codec_encode`/…, `image_encode`/…,
//! `audio_encode`/…, `video_encode`/…, `text_compress`/`text_decompress`, etc.).

pub mod arith;
pub mod audiocodec;
pub mod auto;
pub mod calic;
pub mod columnar;
pub mod csvcolumnar;
pub mod ctxcoder;
pub mod floatcodec;
pub mod imagecodec;
pub mod predictors;
pub mod textcodec;
pub mod transform;
pub mod videocodec;
pub mod zlibw;
