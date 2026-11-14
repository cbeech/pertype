# Productization plan

Turning the validated research codec into an installable, runnable tool. Three phases —
**all shipped**. Releases through **`v0.1.2`** are published on all three channels
(v0.1.2 = first release uniformly shipping fastqcodec):
- GitHub binaries — `github.com/cbeech/pertype/releases/tag/v0.1.2`
- PyPI — `pip install pertype` (https://pypi.org/project/pertype/0.1.2/)
- crates.io — `cargo install pertype` (https://crates.io/crates/pertype/0.1.2)

The install story is complete on every channel.

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
  license. Publishing is live on PyPI and crates.io; future version tags use the automated
  Trusted Publishing workflow (subject to the registry-side publisher configuration).

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
- **`.github/workflows/release.yml`** — on a `v*` tag, a matrix builds the binaries and attaches
  the archives (+ SHA-256) to a GitHub Release. Targets: `x86_64-unknown-linux-musl` and
  `aarch64-unknown-linux-musl` (both fully static), `x86_64-pc-windows-msvc`,
  `x86_64-apple-darwin` (Intel), `aarch64-apple-darwin` (Apple Silicon). ARM64 Linux uses a musl
  cross-container so `lzma-sys` builds with its matching C toolchain; verified on Linux as a static
  AArch64 build with an emulated lossless round-trip.
- **`.github/workflows/ci.yml`** — build + test (Rust + Python, sans the slow parity suite) plus
  the aarch64-musl cross-build and static-linkage check on every push/PR, so a release tag is never
  the first build.
- The binary covers `train`/`compress`/`decompress` + auto-routing (text/byte/CSV/columnar/
  telemetry — patent-clean, no video). The image/audio/video/scientific codecs stay in the
  Python `pip` package. Both install paths documented in the README.
- **`.github/workflows/publish.yml`** — on the same `v*` tag, builds and publishes the Python
  sdist/wheel to PyPI and the crate to crates.io using OIDC Trusted Publishing (no stored registry
  tokens). It no longer waits for `release: published`: releases created with `GITHUB_TOKEN` do not
  emit follow-on workflow events. Manual dispatch remains available as a fallback; its `verify_only`
  input proves both registry authentication paths without uploading new artifacts (PyPI skips
  existing files and Cargo uses `--dry-run`).
- Remaining (optional, post-first-release): see "Deferred distribution items" below.

## Deferred distribution items

The following were identified as post-first-release polish. They are now **explicitly deferred**
under roadmap decision D1 (pertype receives bug-fix attention only; no new features or packaging
work until a concrete integrator or release justifies it):

| Item | Why deferred |
|------|--------------|
| **Homebrew tap / Scoop / winget manifests** | Package-manager manifests are nice-to-have, but `cargo install pertype`, `pip install pertype`, and the GitHub Release binary already cover the three main install paths. Revisit for `v0.2.0` if there is user demand. |
| **`cargo binstall` metadata** | Depends on the above manifests being worth maintaining; defer with them. |
| **Python wheels via `cibuildwheel`** | The sdist + wheel published to PyPI already install on all platforms; pre-built wheels would only speed up installs. Revisit if install-time compilation becomes a support burden. |

The three channels (GitHub Release, PyPI, crates.io) are live and sufficient for the current release.

## Done since this plan
- **`v0.1.0` GitHub Release cut** (2026-06-22) — the release matrix produced all four per-OS
  binaries; verified end-to-end (checksums, archive contents, lossless round-trip). Three
  packaging bugs were found + fixed in the process (Intel-Mac runner scarcity → cross-build on
  Apple Silicon; binary nesting in the `pertype/` package dir → clean staging dir; self-
  referential Windows checksum → checksum by explicit name).
- **Published to PyPI + crates.io** (2026-06-22) — `pip install pertype` and
  `cargo install pertype` are live. Added `.github/workflows/publish.yml` (OIDC Trusted
  Publishing) for tokenless future releases. The install story is complete on all three channels.
- **Automatic registry publishing wired** (2026-09-13) — `publish.yml` now starts directly from
  `v*` tag pushes, in parallel with the binary release workflow. The account-private publisher
  settings must still be configured at both registries; the next version tag is the end-to-end test.

## Still out of scope (external / the user's call)
- A docs site. The FTO search above (an attorney's job). Trusted Publishing requires the one-time
  publisher configuration on each registry site (the user's accounts); its presence is not publicly
  verifiable from this repository.
