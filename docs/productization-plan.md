# Productization plan

Turning the validated research codec into an installable, runnable tool. Three phases —
**all shipped** (license deferred; publishing to PyPI/crates.io is gated on that choice).

## Phase 1 — Installable & usable ✅
- `pyproject.toml` (PEP 621): metadata, `pertype` console entry point, optional-dependency
  extras (`image` / `audio` / `science` / `video` / `all` / `dev`) so the zero-dep text/byte
  core installs clean and the specialist codecs pull their own deps.
- `pertype/__main__.py` so `python -m pertype` works.
- `__version__` in `pertype/__init__.py`.
- README **Quickstart** (install + the handful of commands that matter).
- Native acceleration (`_native/*.c`) shipped as package data; the lazy gcc build still works
  where the install is writable, falls back to pure Python otherwise.
- **License: decided — AGPL-3.0-or-later + commercial (dual).** `LICENSE` (AGPL text),
  `COMMERCIAL.md` (the paid offer + contact), `CLA.md` (contributor relicensing grant); SPDX
  `license` set in `pyproject.toml` (PEP 639) and `Cargo.toml`, SPDX headers on the entry
  points, README licensing section. Open-source for everyone; closed/SaaS use buys a commercial
  license. Publishing is now unblocked (PyPI/crates.io accounts + the publish run remain).

## Phase 2 — Unified UX ✅
- The routing brain already exists (`detect.identify`) and `auto` already covers most
  specialist codecs. Promote `auto` to default top-level `compress` / `decompress` so one
  command "just works", with `--model` to opt into the trained text codec. Surface `identify`.

## Phase 3 — Rust distribution ✅
- Fix `rust/Cargo.toml` metadata (description, license, repository, keywords, categories,
  readme); a unified `pertype` binary exposing the full codec (not just the `azc` subset);
  crates.io-ready (publish gated on the license decision).

## IP due diligence (before the commercial release)

A technical IP review (copyright / patents / dependency licences; not legal advice) found:
- **Copyright: clean** — no copied code; all external names are algorithm/paper citations; no
  vendored source; no committed data files.
- **Dependency licences: clean** — bundled Rust crates are all permissive (AGPL-compatible);
  optional Python extras aren't redistributed (see `THIRD-PARTY-NOTICES.md`; three carry
  GPL/LGPL native-lib caveats for *commercial* users — ffmpeg/x264, LibRaw, libsndfile).
- **Patents: low overall** — built on expired/public-domain foundations (WNC arithmetic, LZ77,
  Rice, JPEG-LS & CALIC patents both expired ~2015, LMS); ANS deliberately avoided (sidesteps
  its live patent thicket). **One elevated area: video motion compensation** (the densest patent
  domain — though H.264/HEVC pools target conformant bitstreams, which this codec does not
  produce), then the two post-2010 techniques (Gorilla XOR-delta, FPC/FCM-DFCM).

**Action before commercial release:** commission a professional **freedom-to-operate (FTO)
search** from a patent attorney, **scoped to the video path first**. If the initial commercial
offering excludes the video codec, residual patent risk drops substantially and a lighter review
suffices. The text/image/audio/arithmetic core is the lowest-priority area for paid review.

## Cross-platform distribution — wired

The standalone `pertype` CLI ships as a single self-contained binary per OS, built by CI:
- **`.github/workflows/release.yml`** — on a `v*` tag, a matrix builds the binary natively on
  Linux / Windows / macOS runners and attaches the archives (+ SHA-256) to a GitHub Release.
  Targets: `x86_64-unknown-linux-musl` (fully static, any distro), `x86_64-pc-windows-msvc`,
  `x86_64-apple-darwin` (Intel), `aarch64-apple-darwin` (Apple Silicon). Pure-Rust deps
  (flate2/rayon, no system libs) → no cross toolchains needed; verified locally as a static musl
  build that round-trips byte-exact.
- **`.github/workflows/ci.yml`** — build + test (Rust + Python, sans the slow parity suite) on
  every push/PR, so a release tag is never the first build.
- The binary covers `train`/`compress`/`decompress` + auto-routing (text/byte/CSV/columnar/
  telemetry — patent-clean, no video). The image/audio/video/scientific codecs stay in the
  Python `pip` package. Both install paths documented in the README.
- Remaining (optional, post-first-release): aarch64-Linux target, Homebrew tap / Scoop / winget
  manifests, `cargo binstall` metadata, Python wheels via `cibuildwheel`.

## Out of scope (this pass)
- Actually publishing to PyPI / crates.io and cutting the first GitHub Release (needs the real
  repo URL + accounts/secrets — the user's call; the CI/release workflows are ready and fire on
  a `v*` tag). A docs site. The FTO search above (an attorney's job).
