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

.PHONY: all test strict asan sharedlib clean
all: test

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
