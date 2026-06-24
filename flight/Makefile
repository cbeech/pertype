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
INC     := -Iinclude
SRC     := $(wildcard src/*.c)
OBJ     := $(SRC:.c=.o)

# Big-endian / cross validation (needs the target toolchain + qemu, e.g.):
#   make CC=powerpc-linux-gnu-gcc CFLAGS='-O2 -static' clean test
#   qemu-ppc ./build/test_pfc          # validates R4 endianness-neutrality on a BE target

.PHONY: all test strict asan sharedlib crosscheck fuzz check clean
all: test

# Independent-decoder cross-check (R7) and decoder robustness fuzz (R6) — Python harnesses.
crosscheck: sharedlib
	python3 test/test_crosscheck.py
fuzz: sharedlib
	python3 test/fuzz_decode.py

# Full local gate: C tests + warnings-as-errors + sanitizers + cross-check + fuzz.
check: strict asan crosscheck fuzz
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
.PHONY: misra
misra:
	@command -v cppcheck >/dev/null 2>&1 || { echo "cppcheck not installed (CI gate skipped)"; exit 0; }
	cppcheck --std=c99 --addon=misra --enable=warning,style --error-exitcode=2 $(INC) src/

clean:
	rm -f src/*.o
	rm -rf build
