# libpfc — pertype-flight host build.
# SPDX-License-Identifier: Apache-2.0
#
# Flight builds cross-compile the same src/ with the target toolchain (see README): the library
# is freestanding-friendly C99 (no malloc, integer-only). This Makefile is the HOST build for
# tests and CI.

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

.PHONY: all test strict asan sharedlib crosscheck fuzz stress check emit clean misra cbmc coverage \
        stackdepth stackdepth-ppc seu mcdc mcdctest
all: test

# Aggressive randomised property + edge + fuzz stress, built under ASan/UBSan (exact-sized buffers).
stress:
	@mkdir -p build
	$(CC) $(CSTD) $(WARN) -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer \
		$(INC) $(SRC) test/stress.c -o build/stress
	./build/stress 5000 150000

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
	./build/test_pfc

# Treat all warnings as errors (MISRA/JPL discipline gate).
strict: CFLAGS += -Werror
strict: clean test

# AddressSanitizer + UndefinedBehaviorSanitizer (decoder eats untrusted input).
asan: CFLAGS += -O1 -g -fsanitize=address,undefined -fno-omit-frame-pointer
asan: clean test

# MISRA-C:2012 static analysis gate (CI). Requires cppcheck with the MISRA addon.
# --suppressions-list applies the project's reviewed deviation policy (see .cppcheck-suppressions
# and docs/misra-deviations.md) -- confirmed tool-limitation false positives and deliberate,
# verified-safe patterns that conflict with an Advisory rule. Anything NOT in that list still
# fails the build; this gate stays meaningful, not silenced wholesale.
misra:
	@command -v cppcheck >/dev/null 2>&1 || { echo "cppcheck not installed (CI gate skipped)"; exit 0; }
	cppcheck --std=c99 --addon=misra --enable=warning,style --error-exitcode=2 \
		--suppressions-list=.cppcheck-suppressions $(INC) src/

# CBMC bounded proof of pfc_block_read (see proofs/cbmc/harness_block_read.c): the shared
# block-framing primitive every codec's decoder calls to parse untrusted downlink data never
# reads outside its src buffer, on a 32-bit size_t target (RAD750/LEON/RISC-V-32) -- NOT the
# 64-bit width this Makefile's every other gate implicitly runs under. --32 is what makes this
# proof mean anything; without it CBMC would silently verify the wrong machine model.
CBMC_CHECKS := --bounds-check --pointer-check --unsigned-overflow-check --signed-overflow-check \
               --unwind 40 --unwinding-assertions
cbmc:
	@command -v cbmc >/dev/null 2>&1 || { echo "cbmc not installed (CI gate skipped)"; exit 0; }
	cbmc --32 --c99 $(INC) \
		proofs/cbmc/harness_block_read.c src/pfc_frame.c src/pfc_crc.c \
		--function harness_pfc_block_read $(CBMC_CHECKS)
	@echo "--- block_read proved; now the symmetric write side ---"
	cbmc --32 --c99 $(INC) \
		proofs/cbmc/harness_block_write.c src/pfc_frame.c src/pfc_crc.c \
		--function harness_pfc_block_write $(CBMC_CHECKS)

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
	@command -v gcovr >/dev/null 2>&1 || { echo "gcovr not installed (CI gate skipped)"; exit 0; }
	rm -rf $(COVDIR)
	@mkdir -p $(COVDIR)
	$(MAKE) $(COVOBJ)
	$(CC) $(COVFLAGS) $(COVOBJ) test/test_pfc.c -o $(COVDIR)/test_pfc
	$(CC) $(COVFLAGS) $(COVOBJ) test/stress.c -o $(COVDIR)/stress
	./$(COVDIR)/test_pfc
	./$(COVDIR)/stress 5000 150000
	gcovr --root . --filter 'src/' --branches --print-summary \
		--fail-under-line 95 --fail-under-branch 80

# MC/DC (modified condition/decision) coverage -- the structural metric DO-178C wants at the
# highest criticality, and strictly stronger than the branch coverage `make coverage` gates.
# Needs clang 18+ (`-fcoverage-mcdc`); gcov cannot measure this at all, which is why it stayed an
# open gap in docs/mission-safety.md §2.2 until now.
#
# MCDC_MIN is a RATCHET, not a compliance target: it sits just under the current measurement so a
# regression fails the build, and is raised as conditions get covered. It is deliberately far below
# 100% -- see docs/requirements.md for the honest gap statement. Do not read a passing `make mcdc`
# as "MC/DC compliant".
MCDC_MIN ?= 62
CLANG ?= clang-18
LLVMCOV ?= llvm-cov-18
LLVMPROF ?= llvm-profdata-18
MCDCDIR := build/mcdc

MCDCFLAGS := -O0 -g -fprofile-instr-generate -fcoverage-mapping -fcoverage-mcdc
mcdc:
	@command -v $(CLANG) >/dev/null 2>&1 || { echo "$(CLANG) not installed (CI gate skipped)"; exit 0; }
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

# The MC/DC-targeted tests also run as ordinary tests in `make check` -- they assert real
# behaviour (each guard rejects what it should), not just coverage shape.
build/test_mcdc: $(OBJ) test/test_mcdc.c
	@mkdir -p build
	$(CC) $(CSTD) $(WARN) $(CFLAGS) $(INC) $(OBJ) test/test_mcdc.c -o $@
mcdctest: build/test_mcdc
	./build/test_mcdc

# SEU (single-event-upset) fault injection into the ENCODER's working memory -- see
# test/seu_inject.c and docs/mission-safety.md §2.5. Measures what no other gate here can: the
# encoder's own pfc_ctx being corrupted mid-encode by radiation, where the CRC is computed AFTER
# the corruption and therefore cannot detect it.
#
# `-Wl,--wrap=pfc_resid_encode` is what makes this possible without putting test-only #ifdef hooks
# into MISRA-reviewed flight source: the linker redirects calls to the harness's __wrap_ function,
# which flips a bit and forwards to __real_. src/ is compiled completely unmodified.
SEU_TRIALS ?= 3000
seu:
	@mkdir -p build
	$(CC) $(CSTD) $(WARN) -O1 -g $(INC) $(SRC) test/seu_inject.c \
		-Wl,--wrap=pfc_resid_encode -o build/seu_inject
	./build/seu_inject $(SEU_TRIALS)

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
	@command -v powerpc-linux-gnu-gcc >/dev/null 2>&1 || \
		{ echo "powerpc cross toolchain not installed (CI gate skipped)"; exit 0; }
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
