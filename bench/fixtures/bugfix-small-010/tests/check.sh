#!/usr/bin/env bash
# ============================================================================
# Test Suite — bugfix-small-010 (cooperative cancellation)
# ============================================================================
# CancellableWorker.cancel()/run(steps) must:
#   * check the flag BEFORE each step: cancelled mid-run -> ("cancelled", n)
#     with n = steps completed, NO further work, NO raise
#   * cancel() before run() -> ("cancelled", 0) immediately
#   * all steps done -> ("completed", len(steps))
#   * cancel() idempotent (double call safe, never raises)
#   * thread-safe: concurrent cancel never lets a step start after the flag
#
# Usage:
#   ./fixtures/bugfix-small-010/tests/check.sh            # broken starter
#   ./fixtures/bugfix-small-010/tests/check.sh /path/to/implementation.py
# Exit code: 0 if all tests pass, 1 if any fail.
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FIXTURE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CANDIDATE="${1:-}"

PASS=0
FAIL=0
pass() { PASS=$((PASS + 1)); }
fail() {
	FAIL=$((FAIL + 1))
	echo "  ❌ $1"
}

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

if [ -n "$CANDIDATE" ]; then
	cp "$CANDIDATE" "$WORKDIR/implementation.py"
else
	cat >"$WORKDIR/implementation.py" <<'PYEOF'
"""BUGGY STARTER for bugfix-small-010."""
import threading


class CancellableWorker:
    def __init__(self):
        self._cancelled = False
        self._lock = threading.Lock()

    def cancel(self):
        with self._lock:
            if self._cancelled:
                raise RuntimeError("already cancelled")
            self._cancelled = True

    def run(self, steps):
        for step in steps:
            step()  # BUG: ignores the flag - always completes every step
        return ("completed", len(steps))
PYEOF
fi

cat >"$WORKDIR/test_runner.py" <<'PYEOF'
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from implementation import CancellableWorker

# NOTE: every assertion below must be a SINGLE builtin-only expression —
# bench_nine.convert_to_pytest copies only `from solution import ...` and the
# test()/test_raises() calls into the verify node's pytest (no module-level
# helpers).

tests = []

def test(name, fn, expected):
    try:
        result = fn()
        ok = result == expected
        tests.append((name, ok, result, expected))
    except Exception as e:
        tests.append((name, False, str(e), expected))

def test_raises(name, fn, exc):
    try:
        fn()
        tests.append((name, False, "no exception raised", exc.__name__))
    except exc:
        tests.append((name, True, exc.__name__, exc.__name__))
    except Exception as e:
        tests.append((name, False, type(e).__name__, exc.__name__))

# 1. completes all steps -> ("completed", len)
test("completes all steps",
     lambda: CancellableWorker().run([lambda: None, lambda: None, lambda: None]),
     ("completed", 3))

# 2. empty steps -> ("completed", 0)
test("empty steps completes zero",
     lambda: CancellableWorker().run([]),
     ("completed", 0))

# 3. cancel before run -> ("cancelled", 0) and NO step executes
test("cancel before run returns cancelled zero",
     lambda: (lambda w: (w.cancel(), w.run([lambda: None]))[1])(CancellableWorker()),
     ("cancelled", 0))

# 4. cancelled worker never runs a post-cancel step that would crash
test("cancel before run skips crashing step",
     lambda: (lambda w: (w.cancel(), w.run([lambda: 1 / 0]))[1])(CancellableWorker()),
     ("cancelled", 0))

# 5. a step that cancels mid-run -> ("cancelled", n) with exact partial count
test("step cancelling itself returns partial count",
     lambda: (lambda w, out: w.run([
         (lambda: (out.append(1), None)[1]),
         (lambda: (out.append(2), w.cancel(), None)[2]),
         (lambda: (out.append(3), None)[1]),
     ]))(CancellableWorker(), []),
     ("cancelled", 2))

# 6. no work after cancel: the post-cancel step never executes (would crash)
test("post-cancel crashing step never runs",
     lambda: (lambda w: w.run([
         (lambda: (w.cancel(), None)[1]),
         (lambda: 1 / 0),
     ]))(CancellableWorker()),
     ("cancelled", 1))

# 7. double cancel is idempotent (starter raises RuntimeError on 2nd call)
test("double cancel is idempotent",
     lambda: (lambda w: (w.cancel(), w.cancel(), w.run([lambda: None]))[2])(CancellableWorker()),
     ("cancelled", 0))

# 8. cancel after completion is safe and result stands
test("cancel after completion safe",
     lambda: (lambda w: (lambda r: (w.cancel(), r)[1])(w.run([lambda: None])))(CancellableWorker()),
     ("completed", 1))

# 9. thread safety: concurrent cancel during a slow run never lets a step
#    start after the flag (5 slow steps; cancel at 0.05s -> only step 1 starts)
test("concurrent cancel stops mid-run",
     lambda: (lambda w, out: (
         (lambda t: (t.start(), __import__("time").sleep(0.05), w.cancel(), t.join(1.0))[-1])(
             __import__("threading").Thread(
                 target=lambda: w.run([(lambda: (out.append(1), __import__("time").sleep(0.2))[1]) for _ in range(5)]))),
         len(out))[1])(CancellableWorker(), []),
     1)

# 10. thread-safety with a crashing post-cancel step: concurrent cancel
#     means the crashing step never starts (no exception escapes run)
test("concurrent cancel avoids crash step",
     lambda: (lambda w, out: (
         (lambda t: (t.start(), __import__("time").sleep(0.05), w.cancel(), t.join(1.0))[-1])(
             __import__("threading").Thread(
                 target=lambda: w.run([(lambda: (out.append(1), __import__("time").sleep(0.2))[1]) for _ in range(3)] + [lambda: 1 / 0]))),
         len(out))[1])(CancellableWorker(), []),
     1)

failed = 0
for name, ok, result, expected in tests:
    status = "PASS" if ok else "FAIL"
    if not ok:
        failed += 1
        print(f"  ❌ {status} - {name}")
        print(f"       got:      {result!r}")
        print(f"       expected: {expected!r}")
    else:
        print(f"  ✅ {status} - {name}")

sys.exit(1 if failed > 0 else 0)
PYEOF

echo "  ── Running tests ───────────────────────────────────────────"
cd "$WORKDIR"
python3 test_runner.py
EXIT_CODE=$?
echo "  ────────────────────────────────────────────────────────────"

if [ "$EXIT_CODE" -eq 0 ]; then
	echo "  ✅ All tests passed"
else
	echo "  ❌ Some tests failed"
fi

exit "$EXIT_CODE"
