"""Dependency-free test runner (pytest works too): python tests/run_tests.py [-k substring]"""
import importlib
import sys
import time
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import conftest  # noqa: F401,E402


def main() -> int:
    key = sys.argv[sys.argv.index("-k") + 1] if "-k" in sys.argv else ""
    passed = failed = 0
    for f in sorted(HERE.glob("test_*.py")):
        mod = importlib.import_module(f.stem)
        for name in sorted(n for n in dir(mod) if n.startswith("test_")):
            if key and key not in name:
                continue
            t0 = time.time()
            try:
                getattr(mod, name)()
                passed += 1
                print(f"PASS {f.stem}.{name} ({time.time() - t0:.1f}s)")
            except Exception:
                failed += 1
                print(f"FAIL {f.stem}.{name}")
                traceback.print_exc()
    print(f"\n{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
