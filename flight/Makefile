# libpfc — pertype-flight host build.
# SPDX-License-Identifier: Apache-2.0
#
# Flight builds cross-compile the same src/ with the target toolchain (see README): the library
# is freestanding-friendly C99 (no malloc, integer-only). This Makefile is the HOST build for
# tests and CI.

# Optional-tool detection, resolved once at parse time.
#
# These exist because several gates need a tool that isn't present in every environment (a bare dev
# box, a cross-compile-only container). The intent has always been "skip cleanly if absent, run if
# present". The previous idiom --
#     @command -v foo >/dev/null 2>&1 || { echo "skipped"; exit 0; }
# -- did NOT do that: `exit 0` ends only THAT recipe line's shell, and make then cheerfully runs the
# next line, so a missing tool printed "skipped" and then failed anyway. Caught when `make coverage`
# died with "gcovr: No such file or directory" immediately after announcing it would be skipped.
# Detecting at parse time and guarding the whole recipe with a make conditional actually skips it.
HAVE_CPPCHECK := $(shell command -v cppcheck 2>/dev/null)
HAVE_CBMC     := $(shell command -v cbmc 2>/dev/null)
HAVE_GCOVR    := $(shell command -v gcovr 2>/dev/null)
HAVE_PPCGCC   := $(shell command -v powerpc-linux-gnu-gcc 2>/dev/null)

CC      ?= cc
CSTD    ?= -std=c99
WARN    := -Wall -Wextra -Wpedantic -Wshadow -Wconversion -Wstrict-prototypes
CFLAGS  ?= -O2
INC     := -Iinclude -Isrc
SRC     := $(wildcard src/*.c)
OBJ     := $(SRC:.c=.o)

# Big-endian / cross validation (needs the target toolchain + qemu, e.g.):
#   make CC=powerpc-linux-gnu-gcc CFLAGS='-O2 -static' clean test
#   qemu-ppc ./build/test_pfc          # validates R4 endianness-neutrality on a BE target

.PHONY: all test strict asan sharedlib crosscheck fuzz stress check emit clean misra cbmc coverage mcdc32 wfsync \
        stackdepth stackdepth-ppc seu mcdc mcdctest containment seudetect
all: test

# Bound every test-binary run.
#
# WHY: the ASan/UBSan runtime can fail NONDETERMINISTICALLY AT STARTUP on kernels with a high
# vm.mmap_rnd_bits (32 on both dev hosts here), and it does so in two different ways depending on
# the compiler. Measured on debian:bookworm, 60 trials each, on a program that is four lines of
# malloc/free -- so this is the runtime, not any code under test:
#
#     gcc 12   + ASan            16/60 HANG
#     clang 14 + ASan+libFuzzer  14/60 SIGSEGV
#     no sanitizer                60/60 clean
#
# The hang is the dangerous one. ASan takes a fatal signal during its own startup, its reporting
# path faults as well, and it recurses -- emitting "AddressSanitizer:DEADLYSIGNAL" forever. One
# captured instance ran 74 minutes on a workload that takes 11 seconds and wrote 8.5 GB / 301
# million lines, so it consumes DISK as well as CPU. Bounding the run converts an indefinite wedge
# into a clean, visible failure a retry can clear; note it bounds the time, not the volume.
# It does NOT mask a real hang in the code under test -- that also deserves to fail.
#
# The cause is the runtime VERSION, not the host: on the same kernel, ubuntu:24.04 measures 60/60
# clean for both compilers where bookworm-vintage images fail 20-27% of the time. Prefer
# ubuntu:24.04 for sanitizer work. Set RUN_TIMEOUT= (empty) to disable this guard.
RUN_TIMEOUT ?= 900
RUN := $(if $(RUN_TIMEOUT),timeout $(RUN_TIMEOUT),)

# Aggressive randomised property + edge + fuzz stress, built under ASan/UBSan (exact-sized buffers).
stress:
	@mkdir -p build
	$(CC) $(CSTD) $(WARN) -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer \
		$(INC) $(SRC) test/stress.c -o build/stress
	$(RUN) ./build/stress 5000 150000

# Independent-decoder cross-check (R7) and decoder robustness fuzz (R6) — Python harnesses.
crosscheck: sharedlib
	python3 test/test_crosscheck.py
fuzz: sharedlib
	python3 test/fuzz_decode.py

# Full local gate: warnings-as-errors + sanitizers + cross-check + fuzz + randomised stress.
check: strict asan mcdctest crosscheck fuzz stress
	@echo "ALL CHECKS PASSED"

# Shared library for the host-side bit-exactness / benchmark bridge (test/bench_real.py).
sharedlib: build/libpfc.so
build/libpfc.so: $(SRC)
	@mkdir -p build
	$(CC) $(CSTD) $(WARN) $(CFLAGS) -fPIC -shared $(INC) $(SRC) -o $@

%.o: %.c
	$(CC) $(CSTD) $(WARN) $(CFLAGS) $(INC) -c $< -o $@

build/test_pfc: $(OBJ) test/test_pfc.c
	@mkdir -p build
	$(CC) $(CSTD) $(WARN) $(CFLAGS) $(INC) $(OBJ) test/test_pfc.c -o $@

test: build/test_pfc
	$(RUN) ./build/test_pfc

# Treat all warnings as errors (MISRA/JPL discipline gate).
strict: CFLAGS += -Werror
strict: clean test

# AddressSanitizer + UndefinedBehaviorSanitizer (decoder eats untrusted input).
asan: CFLAGS += -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer
asan: clean test

# CI workflow drift gate.
#
# .github/workflows/flight-ci.yml and .gitea/workflows/flight-ci.yml are deliberately
# near-identical and kept in sync BY HAND -- which is precisely the arrangement that lets a gate be
# tightened in one copy and silently left weak in the other. That failure is invisible: both files
# stay valid YAML and both keep passing, while the runner you actually push to quietly tests less
# than you think. A plain diff cannot police it, because the two are SUPPOSED to differ in their
# headers and path filters, so the tool compares job/step structure and ignores prose.
wfsync:
	python3 tools/workflow_sync.py

# MISRA-C:2012 static analysis gate (CI). Requires cppcheck with the MISRA addon.
# --suppressions-list applies the project's reviewed deviation policy (see .cppcheck-suppressions
# and docs/misra-deviations.md) -- confirmed tool-limitation false positives and deliberate,
# verified-safe patterns that conflict with an Advisory rule. Anything NOT in that list still
# fails the build; this gate stays meaningful, not silenced wholesale.
misra:
ifeq ($(HAVE_CPPCHECK),)
	@echo "cppcheck not installed (CI gate skipped)"
else
	cppcheck --std=c99 --addon=misra --enable=warning,style --error-exitcode=2 \
		--suppressions-list=.cppcheck-suppressions $(INC) src/
endif

# CBMC bounded proof of pfc_block_read (see proofs/cbmc/harness_block_read.c): the shared
# block-framing primitive every codec's decoder calls to parse untrusted downlink data never
# reads outside its src buffer, on a 32-bit size_t target (RAD750/LEON/RISC-V-32) -- NOT the
# 64-bit width this Makefile's every other gate implicitly runs under. --32 is what makes this
# proof mean anything; without it CBMC would silently verify the wrong machine model.
CBMC_CHECKS := --bounds-check --pointer-check --unsigned-overflow-check --signed-overflow-check \
               --unwind 40 --unwinding-assertions
cbmc:
ifeq ($(HAVE_CBMC),)
	@echo "cbmc not installed (CI gate skipped)"
else
	cbmc --32 --c99 $(INC) \
		proofs/cbmc/harness_block_read.c src/pfc_frame.c src/pfc_crc.c \
		--function harness_pfc_block_read $(CBMC_CHECKS)
	@echo "--- block_read proved; now the symmetric write side ---"
	cbmc --32 --c99 $(INC) \
		proofs/cbmc/harness_block_write.c src/pfc_frame.c src/pfc_crc.c \
		--function harness_pfc_block_write $(CBMC_CHECKS)
endif

# Structural coverage (line + branch, via gcov/gcovr). Same corpus as `check`'s functional gates
# (test_pfc + stress): both instrumented binaries link the SAME coverage objects, so running both
# accumulates into shared .gcda counters -- one combined report, not two separate ones. MC/DC is
# NOT measured here (gcov doesn't do condition/decision coverage); see docs/mission-safety.md §2.2
# for why that remains a separate, unaddressed gap. Thresholds are set below the current measured
# baseline (98.4% line / 89.2% branch project-wide, see docs/requirements.md) to leave headroom
# for legitimate new code while still catching a real regression.
COVDIR := build/cov
COVFLAGS := $(CSTD) $(WARN) --coverage -O0 -g $(INC)
COVOBJ := $(patsubst src/%.c,$(COVDIR)/%.o,$(SRC))

$(COVDIR)/%.o: src/%.c
	@mkdir -p $(COVDIR)
	$(CC) $(COVFLAGS) -c $< -o $@

coverage:
ifeq ($(HAVE_GCOVR),)
	@echo "gcovr not installed (CI gate skipped)"
else
	rm -rf $(COVDIR)
	@mkdir -p $(COVDIR)
	$(MAKE) $(COVOBJ)
	$(CC) $(COVFLAGS) $(COVOBJ) test/test_pfc.c -o $(COVDIR)/test_pfc
	$(CC) $(COVFLAGS) $(COVOBJ) test/stress.c -o $(COVDIR)/stress
	./$(COVDIR)/test_pfc
	./$(COVDIR)/stress 5000 150000
	gcovr --root . --filter 'src/' --branches --print-summary \
		--fail-under-line 95 --fail-under-branch 80
endif

# MC/DC (modified condition/decision) coverage -- the structural metric DO-178C wants at the
# highest criticality, and strictly stronger than the branch coverage `make coverage` gates.
# Needs clang 18+ (`-fcoverage-mcdc`); gcov cannot measure this at all, which is why it stayed an
# open gap in docs/mission-safety.md §2.2 until now.
#
# MCDC_MIN is a RATCHET, not a compliance target: it sits just under the current measurement so a
# regression fails the build, and is raised as conditions get covered. It is deliberately far below
# 100% -- see docs/requirements.md for the honest gap statement. Do not read a passing `make mcdc`
# as "MC/DC compliant".
# Measured 89.57% (103/115 conditions) on 2026-08-25, verified on the craig@ai Linux host.
# History worth keeping: this sat at 73 while test_mcdc.c grew by 97 assertions. A floor well below
# the measurement does not merely fail to protect new coverage -- it hides whether that coverage
# ever landed. It had not: a re-measurement found 5 of a claimed 17 conditions actually covered.
# Raise this whenever the measurement rises.
#
# The remaining 13 uncovered conditions are documented in docs/requirements.md as unreachable by
# construction (32-bit-only pfc_size_mul guards, the store-raw pos>=raw_bytes pairing, the
# range-coder renorm underflow, and one pair of COUPLED conditions in pfc_image.c's tie-break),
# not as missing tests.
MCDC_MIN ?= 89
CLANG ?= clang-18
LLVMCOV ?= llvm-cov-18
LLVMPROF ?= llvm-profdata-18
MCDCDIR := build/mcdc

MCDCFLAGS := -O0 -g -fprofile-instr-generate -fcoverage-mapping -fcoverage-mcdc
HAVE_CLANG := $(shell command -v $(CLANG) 2>/dev/null)
mcdc:
ifeq ($(HAVE_CLANG),)
	@echo "$(CLANG) not installed (CI gate skipped)"
else
	@rm -rf $(MCDCDIR)
	@mkdir -p $(MCDCDIR)
	$(CLANG) $(CSTD) $(MCDCFLAGS) $(INC) $(SRC) test/test_pfc.c  -o $(MCDCDIR)/test_pfc
	$(CLANG) $(CSTD) $(MCDCFLAGS) $(INC) $(SRC) test/stress.c    -o $(MCDCDIR)/stress
	$(CLANG) $(CSTD) $(MCDCFLAGS) $(INC) $(SRC) test/test_mcdc.c -o $(MCDCDIR)/test_mcdc
	cd $(MCDCDIR) && LLVM_PROFILE_FILE=test.profraw ./test_pfc >/dev/null
	cd $(MCDCDIR) && LLVM_PROFILE_FILE=stress.profraw ./stress 5000 150000 >/dev/null
	cd $(MCDCDIR) && LLVM_PROFILE_FILE=mcdc.profraw ./test_mcdc >/dev/null
	cd $(MCDCDIR) && $(LLVMPROF) merge -sparse test.profraw stress.profraw mcdc.profraw \
		-o merged.profdata
	cd $(MCDCDIR) && $(LLVMCOV) export ./test_pfc -object ./stress -object ./test_mcdc \
		-instr-profile=merged.profdata --format=text > cov.json
	python3 tools/mcdc_gate.py $(MCDCDIR)/cov.json --min-percent $(MCDC_MIN)
endif

# 32-bit MC/DC (R10-adjacent, and the architecture that actually flies).
#
# WHY A SECOND MEASUREMENT: eight of the conditions `make mcdc` reports as uncovered are the
# pfc_size_mul capacity guards, and docs/requirements.md classifies them as unreachable *on a
# 64-bit model by construction* -- e.g. pfc_image.c's width*height*es cannot overflow a 64-bit
# size_t given width <= PFC_MAX_COLS and es <= 2. That classification is correct and it is also
# the wrong architecture to stop at: libpfc's encoder is specified to run on 32-bit flight targets
# (RAD750 / LEON / RISC-V-32, see pfc.h), where those products CAN wrap and the guards are live.
#
# So this target is not a coverage-percentage exercise. It exercises, on the deployment word size,
# the exact guards that exist because of it -- the same reason the cbmc gate runs under --32 rather
# than the host model, and the same class of bug that CBMC proof found in pfc_block_read.
#
# Needs 32-bit libc headers: libc6-dev-i386 (Debian/Ubuntu) or gcc-multilib.
# MCDC32_MIN is a separate ratchet from MCDC_MIN: the two builds reach different conditions, so a
# single floor across both would be meaningless. Measured 93.91% (108/115) here against 89.57%
# (103/115) on the 64-bit build -- the difference is exactly the pfc_size_mul guards, which
# test_mcdc.c's mcdc_size_mul_overflow() drives with headers whose products wrap a 32-bit size_t
# but not a 64-bit one. Those cases assert the same PFC_E_BOUND on both word sizes (on 64-bit the
# `cap < total` half of the same condition rejects them), so there is no #ifdef anywhere.
MCDC32DIR := build/mcdc32
MCDC32FLAGS := -m32 -O0 -g -fprofile-instr-generate -fcoverage-mapping -fcoverage-mcdc
MCDC32_MIN ?= 93

mcdc32:
ifeq ($(HAVE_CLANG),)
	@echo "$(CLANG) not installed (CI gate skipped)"
else
	@rm -rf $(MCDC32DIR)
	@mkdir -p $(MCDC32DIR)
	$(CLANG) $(CSTD) $(MCDC32FLAGS) $(INC) $(SRC) test/test_pfc.c  -o $(MCDC32DIR)/test_pfc
	$(CLANG) $(CSTD) $(MCDC32FLAGS) $(INC) $(SRC) test/stress.c    -o $(MCDC32DIR)/stress
	$(CLANG) $(CSTD) $(MCDC32FLAGS) $(INC) $(SRC) test/test_mcdc.c -o $(MCDC32DIR)/test_mcdc
	cd $(MCDC32DIR) && LLVM_PROFILE_FILE=test.profraw ./test_pfc >/dev/null
	cd $(MCDC32DIR) && LLVM_PROFILE_FILE=stress.profraw ./stress 5000 150000 >/dev/null
	cd $(MCDC32DIR) && LLVM_PROFILE_FILE=mcdc.profraw ./test_mcdc >/dev/null
	cd $(MCDC32DIR) && $(LLVMPROF) merge -sparse test.profraw stress.profraw mcdc.profraw 		-o merged.profdata
	cd $(MCDC32DIR) && $(LLVMCOV) export ./test_pfc -object ./stress -object ./test_mcdc 		-instr-profile=merged.profdata --format=text > cov.json
	python3 tools/mcdc_gate.py $(MCDC32DIR)/cov.json --min-percent $(MCDC32_MIN)
endif

# The MC/DC-targeted tests also run as ordinary tests in `make check` -- they assert real
# behaviour (each guard rejects what it should), not just coverage shape.
build/test_mcdc: $(OBJ) test/test_mcdc.c
	@mkdir -p build
	$(CC) $(CSTD) $(WARN) $(CFLAGS) $(INC) $(OBJ) test/test_mcdc.c -o $@
mcdctest: build/test_mcdc
	$(RUN) ./build/test_mcdc

# Downlink-corruption containment (R6): flip one bit in one block's payload, then measure whether
# the damage stays in that block. Reports rather than asserts, because the answer is currently
# codec-dependent and one of the answers is "no": IMAGE contains it, SPECTRAL propagates across
# every subsequent band (see docs/mission-safety.md 2.5). Run alongside `seu` -- that one covers
# encoder-side faults, this one covers the channel-side faults R6 is actually written about.
containment:
	@mkdir -p build
	$(CC) $(CSTD) $(WARN) -O1 -g $(INC) $(SRC) test/downlink_containment.c -o build/containment
	./build/containment

# Measures whether a cheap software self-check could make encoder-side SEU DETECTABLE.
# Answer, recorded here so the idea is not retried blind: no -- only 19.7% of silent corruptions
# are caught, because pfc_model_rescale recomputes tot from freq and thereby launders the
# corruption, and mant[]/bias_*[] carry no redundancy at all. See docs/mission-safety.md 2.5.
# Kept as a gate-able experiment so the conclusion can be re-verified if the model ever changes.
seudetect:
	@mkdir -p build
	$(CC) $(CSTD) $(WARN) -O2 $(INC) $(SRC) test/seu_detect.c 		-Wl,--wrap=pfc_resid_encode -Wl,--wrap=pfc_model_reset -o build/seu_detect
	./build/seu_detect $(SEU_TRIALS)

# SEU (single-event-upset) fault injection into the ENCODER's working memory -- see
# test/seu_inject.c and docs/mission-safety.md §2.5. Measures what no other gate here can: the
# encoder's own pfc_ctx being corrupted mid-encode by radiation, where the CRC is computed AFTER
# the corruption and therefore cannot detect it.
#
# `-Wl,--wrap=pfc_resid_encode` is what makes this possible without putting test-only #ifdef hooks
# into MISRA-reviewed flight source: the linker redirects calls to the harness's __wrap_ function,
# which flips a bit and forwards to __real_. src/ is compiled completely unmodified.
SEU_TRIALS ?= 3000
SEU_BURST  ?= 1
seu:
	@mkdir -p build
	$(CC) $(CSTD) $(WARN) -O1 -g $(INC) $(SRC) test/seu_inject.c \
		-Wl,--wrap=pfc_resid_encode -o build/seu_inject
	./build/seu_inject $(SEU_TRIALS) $(SEU_BURST)

# Worst-case stack-depth bound (see tools/stack_depth.py and docs/mission-safety.md §2.4).
# Flight software must bound maximum stack usage; for libpfc that bound is EXACT rather than
# estimated, because the two properties that make the general problem undecidable are both already
# forbidden here and are asserted by the tool: no recursion (so the call graph is a DAG and
# "deepest path" terminates) and no function pointers (so the call graph is complete). If either
# ever regresses, `stackdepth` fails loudly instead of quietly reporting a wrong number.
#
# STACK_BUDGET is a regression gate, not a hardware limit: it sits well above both measured targets
# (x86-64 and PowerPC, see docs/requirements.md) so it catches a structural blowup -- a new deep
# call chain -- without tripping on ordinary codegen drift between compiler versions.
STACK_BUDGET ?= 1024
STACKDIR := build/stack
STACK_ENTRIES := --entry pfc_encode --entry pfc_decode

# GCC writes each translation unit's .su next to its -o target, so they collect in $(STACKDIR).
stackdepth:
	@rm -rf $(STACKDIR)
	@mkdir -p $(STACKDIR)
	@for f in $(SRC); do \
		$(CC) $(CSTD) $(CFLAGS) -fstack-usage $(INC) -c $$f \
			-o $(STACKDIR)/`basename $${f%.c}`.o || exit 1; \
	done
	python3 tools/stack_depth.py --build-dir $(STACKDIR) --objects $(STACKDIR)/*.o \
		--label "$(CC) $(CFLAGS)" $(STACK_ENTRIES) --max-bytes $(STACK_BUDGET)

# Same analysis against the real flight target's ABI (big-endian 32-bit PowerPC, RAD750-class).
# Worth running separately because frame layout is ABI-specific -- the host number is not a proxy
# for the flight number, and in practice PowerPC's frames differ measurably from x86-64's.
stackdepth-ppc:
ifeq ($(HAVE_PPCGCC),)
	@echo "powerpc cross toolchain not installed (CI gate skipped)"
else
	@rm -rf $(STACKDIR)-ppc
	@mkdir -p $(STACKDIR)-ppc
	@for f in $(SRC); do \
		powerpc-linux-gnu-gcc $(CSTD) $(CFLAGS) -fstack-usage $(INC) -c $$f \
			-o $(STACKDIR)-ppc/`basename $${f%.c}`.o || exit 1; \
	done
	python3 tools/stack_depth.py --objdump powerpc-linux-gnu-objdump \
		--build-dir $(STACKDIR)-ppc --objects $(STACKDIR)-ppc/*.o \
		--label "powerpc-linux-gnu-gcc $(CFLAGS) (flight target ABI)" \
		$(STACK_ENTRIES) --max-bytes $(STACK_BUDGET)
endif

# Deterministic fixture emitter — the R4 endianness-neutrality proof. Built once natively (host
# endianness) and once cross-compiled for a big-endian target (CI job `bigendian`), each run's
# stdout is byte-compared: identical bytes means a BE flight encoder and an LE ground decoder
# agree on the wire format. `make emit` alone just builds+runs it for the host (sanity check);
# the actual BE comparison needs a cross toolchain + qemu-user and lives in flight-ci.yml.
build/emit: $(OBJ) test/emit.c
	@mkdir -p build
	$(CC) $(CSTD) $(WARN) $(CFLAGS) $(INC) $(OBJ) test/emit.c -o $@
emit: build/emit
	./build/emit > build/emit.out
	@echo "wrote build/emit.out ($$(wc -c < build/emit.out) bytes)"

clean:
	rm -f src/*.o
	rm -rf build
