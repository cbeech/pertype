"""Tiny zero-dependency test runner.

Discovers every callable named ``test_*`` in the ``tests`` package modules and
runs it, reporting failures with tracebacks. Usage::

    python3 -m tests.run            # run all test modules
    python3 -m tests.run huffman    # run only tests/test_huffman.py
"""
import importlib
import pkgutil
import sys
import traceback

import tests


def discover_modules(filter_name=None):
    for info in pkgutil.iter_modules(tests.__path__):
        if not info.name.startswith("test_"):
            continue
        if filter_name and filter_name not in info.name:
            continue
        yield importlib.import_module(f"tests.{info.name}")


def main(argv):
    filter_name = argv[1] if len(argv) > 1 else None
    passed = failed = 0
    failures = []
    for module in discover_modules(filter_name):
        for name in sorted(dir(module)):
            if not name.startswith("test_"):
                continue
            fn = getattr(module, name)
            if not callable(fn):
                continue
            label = f"{module.__name__}.{name}"
            try:
                fn()
                passed += 1
                print(f"  ok   {label}")
            except Exception:  # noqa: BLE001 - report any test failure
                failed += 1
                failures.append((label, traceback.format_exc()))
                print(f"  FAIL {label}")
    print(f"\n{passed} passed, {failed} failed")
    for label, tb in failures:
        print(f"\n===== {label} =====\n{tb}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
