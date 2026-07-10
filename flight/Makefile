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

.PHONY: all test strict asan sharedlib crosscheck fuzz stress check emit clean misra cbmc
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
check: strict asan crosscheck fuzz stress
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
cbmc:
	@command -v cbmc >/dev/null 2>&1 || { echo "cbmc not installed (CI gate skipped)"; exit 0; }
	cbmc --32 $(CSTD) $(INC) \
		proofs/cbmc/harness_block_read.c src/pfc_frame.c src/pfc_crc.c \
		--function harness_pfc_block_read \
		--bounds-check --pointer-check --unsigned-overflow-check --signed-overflow-check \
		--unwind 40 --unwinding-assertions

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
