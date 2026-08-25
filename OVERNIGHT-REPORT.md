# Overnight run — CI validation slate (2026-08-26)

Branch: `master`. **No pushing this run** — the user chose the `overnight` skill's default hard
rule at kickoff, so everything below stays as local commits. Nothing left this machine.

Previous run archived to `OVERNIGHT-REPORT-2026-08-25.md`.

## Why this queue

`cad952f` shipped a CI change I explicitly could **not** verify: `container: node:22-trixie` on the
`native`/`libfuzzer` jobs and a new `mcdc32` job. The `make` targets were proven on `craig@ai`, but
whether act — which is what the self-hosted Gitea runner actually is — honours `container:` and
parses the new job was untested. That is the known unknown, so it goes first.

Per the user's standing instruction, **all test execution happens on `craig@ai`**, not local
Docker-on-WSL2.

## Queue

| # | Task | Status |
|---|------|--------|
| 1 | Validate act's `container:` support and the `mcdc32` job | running |
| 2 | Full 10-gate run on `ai` at `cad952f` | queued |
| 3 | Workflow structure-sync checker (ignoring prose) | queued |
| 4 | Long libFuzzer soak in `node:22-trixie` | queued |
| 5 | Wrap-up | queued |

## Needs you (not queued)

- **Pushing** — nothing reached any remote this run, by the kickoff decision.
- The three open `TODO.md` items — features, blocked by D1.
- RG2.1, RG4.1, R5 — need a person, funding, or hardware.
- `Recycle/2026-08-24-stray-dir-flight-semicolon-C` — staged, not deleted.
- `sysctl -w vm.mmap_rnd_bits=28` — fixes the ASan cause rather than routing around it.

---

## Results

