# Overnight report — flight-core toolchain-gated functionality

Started via /overnight. Branch: `flight-core`. Environment: Windows Git Bash at `C:\Dev\compression`
— NO native C toolchain available (no gcc/cc/make), Docker daemon not running. Per explicit user
caution ("computer crashed, do this later when it isn't so busy") and to avoid retriggering
instability, this run does NOT start Docker/WSL or attempt any native/heavy compilation. Scope is
limited to work verifiable by code review + config validation: a CI workflow that runs the
toolchain-gated gates (MISRA, libFuzzer, big-endian cross-compile+qemu) on a GitHub Actions runner,
which has root and the needed toolchains — none of that execution happens on this machine tonight.

**Never pushed. All commits stay local on `flight-core`.**

---
## Task 1: Review + integrate `flight/test/emit.c` — DONE

**Found a real bug via code review** (verified by C-language reasoning, not execution — no
toolchain available tonight): `emit.c` declared `static struct pfc_ctx g_work;`, but `pfc_ctx` is
forward-declared as an OPAQUE type in the public `pfc.h` (only `pfc_internal.h` has the full
struct). Instantiating an incomplete type at file scope is a hard C compile error — this file
would not have compiled as committed. `PFC_WORKMEM_BYTES` also expands to a function call
`(pfc_workmem_bytes())`, not a compile-time constant, so it can't size a static array either.

**Fix:** matched the established pattern already used correctly in `test_pfc.c`/`stress.c`/
`fuzz_pfc.c` — `static struct pfc_ctx *g_work;` + `g_work = malloc(pfc_workmem_bytes());` once at
startup (R2 governs the *library's* internal allocation, not a one-time caller-side setup).
Updated the misleading "static — no malloc" comment to explain this honestly, and to explain why
a literal static instantiation isn't possible against the public header.

Also verified by full read-through: `pfc_params` fields are fully set before each of the 3
`emit()` calls (image/seq/spectral), buffer sizes match `width*height*count*elemsize` exactly,
the LCG is deterministic (no time/rand), and the 4-byte length prefix is hand-packed
little-endian (not a host struct dump) — genuinely endian-portable on the writing side too.

Added a `build/emit` + `emit` Makefile target (mirrors the `build/test_pfc` pattern exactly;
verified real tabs via `cat -A`, no CRLF/space corruption). Documented in `README.md`.

**Verification status:** code-reviewed and reasoned correct with high confidence (opaque-type
and constant-expression rules are unambiguous C semantics); **NOT** locally compiled (no
toolchain in this shell tonight) — first real compile will happen in CI. Marked accordingly, not
claimed as tested.

Files: `flight/test/emit.c`, `flight/Makefile`, `flight/README.md`.

