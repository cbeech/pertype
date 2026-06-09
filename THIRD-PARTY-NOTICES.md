# Third-party notices

This project (the `compressor` Python package and the `compressor_rs` Rust crate) is original
work, dual-licensed AGPL-3.0-or-later + commercial (see [`LICENSE`](LICENSE) and
[`COMMERCIAL.md`](COMMERCIAL.md)). It contains **no vendored third-party source code**. It does,
however, *depend on* third-party software in two distinct ways, with different obligations:

- **Bundled** — compiled/linked into the shipped Rust artifact, so their licenses travel with
  a distribution of this project.
- **Installed separately** — Python *optional-dependency extras* that `pip` fetches onto the
  user's machine at install time. This project does **not** redistribute them; their licenses
  bind the user's environment, not this project's distribution.

The algorithm names cited in the source and docs (Witten–Neal–Cleary, JPEG-LS / LOCO-I, CALIC,
Paeth, COVER, Monkey's Audio, Gorilla, FPC/FCM-DFCM, etc.) are **references to published
techniques**, not third-party code — every codec here is an independent implementation.

## Bundled — Rust crates (permissive, attribution only)

The Rust build's entire dependency closure is permissively licensed (MIT / Apache-2.0 / Zlib /
0BSD); each offers an MIT-or-equivalent option, so there is no copyleft and the Apache-2.0
patent-clause question is moot. Their copyright/licence notices are preserved by Cargo's build.

| Crate | License |
|---|---|
| flate2 | MIT OR Apache-2.0 |
| miniz_oxide | MIT OR Zlib OR Apache-2.0 |
| simd-adler32 | MIT |
| adler2 | 0BSD OR MIT OR Apache-2.0 |
| crc32fast | MIT OR Apache-2.0 |
| rayon, rayon-core | MIT OR Apache-2.0 |
| crossbeam-deque, crossbeam-epoch, crossbeam-utils | MIT OR Apache-2.0 |
| cfg-if | MIT OR Apache-2.0 |
| either | MIT OR Apache-2.0 |

GPLv3/AGPLv3 are one-directionally compatible with Apache-2.0, so mixing these into the AGPL
work is fine; for a commercial build they impose only the usual "preserve notices" requirement.

The Python text/byte core has **zero** runtime dependencies. The native acceleration compiles
this project's *own* C sources (`compressor/_native/*.c`) with the system `gcc` at runtime — no
third-party code is compiled or linked (GCC's runtime-library exception means compiler use does
not affect the output's licensing).

## Installed separately — Python optional extras

These are declared in `pyproject.toml` `[project.optional-dependencies]` and pulled by, e.g.,
`pip install "compressor[all]"`. They are not part of this project's distribution. Most are
permissive (numpy — BSD-3-Clause; pillow — MIT-CMU; imagecodecs — BSD-3-Clause, whose
GPL-capable codecs are source-only and not built by default; pytest — MIT, dev-only). **Three
carry native-library obligations worth knowing about:**

- **`video` extra → `imageio-ffmpeg` (BSD-2-Clause) downloads a GPL ffmpeg.** imageio-ffmpeg's
  default build includes **libx264 (GPLv2+)**, which makes the downloaded ffmpeg binary
  effectively **GPL**. This is fine for AGPL use and you do not redistribute the binary — but a
  **commercial, closed-source** product built around this extra would inherit ffmpeg/x264's GPL
  terms on the binary it ships. Such users should supply their own **LGPL** ffmpeg build (no
  x264) or obtain an x264 commercial license.
- **`image` extra → `rawpy` (MIT) wraps LibRaw (LGPL-2.1-or-later / CDDL-1.0).** LGPL; usable in
  any program, including commercial/closed-source, provided LibRaw stays a replaceable
  (dynamically linked) library and its notices are preserved. rawpy deliberately excludes
  LibRaw's GPL demosaic packs, so no GPL is pulled in.
- **`audio` extra → `soundfile` (BSD-3-Clause) wraps libsndfile (LGPL-2.1+).** Same shape as
  LibRaw: LGPL, dynamically linked, separately installed — fine for AGPL and for commercial use
  under the standard LGPL relinking/notice terms.

**Summary for commercial users:** the project's own code and its *bundled* dependencies are
clean (permissive). If you enable the optional `video` extra in a closed-source product, use a
non-GPL (LGPL) ffmpeg; the `image`/`audio` extras' LGPL native libraries are usable under the
usual LGPL conditions.

*This document is informational, not legal advice. Verify your obligations for your specific
distribution with counsel if in doubt.*
