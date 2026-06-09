# Productization plan

Turning the validated research codec into an installable, runnable tool. Three phases —
**all shipped** (license deferred; publishing to PyPI/crates.io is gated on that choice).

## Phase 1 — Installable & usable ✅
- `pyproject.toml` (PEP 621): metadata, `compressor` console entry point, optional-dependency
  extras (`image` / `audio` / `science` / `video` / `all` / `dev`) so the zero-dep text/byte
  core installs clean and the specialist codecs pull their own deps.
- `compressor/__main__.py` so `python -m compressor` works.
- `__version__` in `compressor/__init__.py`.
- README **Quickstart** (install + the handful of commands that matter).
- Native acceleration (`_native/*.c`) shipped as package data; the lazy gcc build still works
  where the install is writable, falls back to pure Python otherwise.
- **License: TBD** — packaging is scaffolded but the `license` field is intentionally left
  unset (a comment marks it), which blocks any real publish until chosen.

## Phase 2 — Unified UX ✅
- The routing brain already exists (`detect.identify`) and `auto` already covers most
  specialist codecs. Promote `auto` to default top-level `compress` / `decompress` so one
  command "just works", with `--model` to opt into the trained text codec. Surface `identify`.

## Phase 3 — Rust distribution ✅
- Fix `rust/Cargo.toml` metadata (description, license, repository, keywords, categories,
  readme); a unified `compressor` binary exposing the full codec (not just the `azc` subset);
  crates.io-ready (publish gated on the license decision).

## Out of scope (this pass)
- Actually publishing to PyPI / crates.io (needs the license + accounts/secrets — the user's
  call). CI pipelines. A docs site.
