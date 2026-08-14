#!/usr/bin/env bash
# ============================================================================
# Test Suite — bugfix-small-009 (retry-with-backoff edges)
# ============================================================================
# retry_call(fn, attempts, base_delay, backoff, jitter, retryable) must:
#   * return the first successful result, retrying ONLY retryable failures
#   * apply exponential backoff base_delay * backoff**(n-1) before retry n
#   * re-raise the LAST exception after attempts (never return None = silent
#     success), re-raise non-retryable exceptions immediately
#   * reject attempts < 1 and negative delays with ValueError
#
# Usage:
#   ./fixtures/bugfix-small-009/tests/check.sh            # broken starter
#   ./fixtures/bugfix-small-009/tests/check.sh /path/to/implementation.py
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
"""BUGGY STARTER for bugfix-small-009."""
import time


def retry_call(fn, *, attempts=3, base_delay=0.1, backoff=2.0, jitter=False,
               retryable=(Exception,)):
    for _ in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - starter bug: swallows ALL
            last = exc
    # BUG: no backoff delay, returns None on final failure
    return None
PYEOF
fi

cat >"$WORKDIR/test_runner.py" <<'PYEOF'
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from implementation import retry_call

# NOTE: every assertion below must be a SINGLE builtin-only expression —
# bench_nine.convert_to_pytest copies only `from solution import ...` and the
# test()/test_raises() calls into the verify node's pytest (no module-level
# helpers). Raise-inside-expression idiom: (lambda: (_ for _ in ()).throw(E))()

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

# 1. succeeds on the first call -> exactly the value, no retries needed
test("succeeds on first call",
     lambda: retry_call(lambda: "ok", attempts=3, base_delay=0.0),
     "ok")

# 2. succeeds on the 3rd call -> value returned AND exactly 3 calls
test("succeeds on third call",
     lambda: (lambda c=[0]: (
         retry_call((lambda: (c.__setitem__(0, c[0] + 1),
                              "ok" if c[0] >= 3 else (lambda: (_ for _ in ()).throw(IOError("t")))())[1]),
                    attempts=3, base_delay=0.0),
         c[0])[1] == 3)(),
     True)

# 3. exponential backoff: attempts=3, base_delay=0.02, backoff=2.0 -> the two
#    retries must wait 0.02 + 0.04 = 0.06s total (assert >= 0.03, jitter off)
test("backoff grows between retries",
     lambda: (lambda t0, c=[0]: (
         retry_call((lambda: (c.__setitem__(0, c[0] + 1),
                              "ok" if c[0] >= 3 else (lambda: (_ for _ in ()).throw(IOError("t")))())[1]),
                    attempts=3, base_delay=0.02, backoff=2.0, jitter=False),
         (__import__("time").time() - t0) >= 0.03 and c[0] == 3)[1])(__import__("time").time()),
     True)

# 4. model-or-fail honesty: after the last attempt the LAST exception is
#    re-raised (never None) - the broken starter returns None here
test_raises("raises last exception after attempts",
            lambda: retry_call(lambda: 1 / 0, attempts=2, base_delay=0.0),
            ZeroDivisionError)

# 5. retryable predicate: a non-retryable IOError(400) is re-raised
#    immediately (no retries, no None) - broken starter retries then returns None
test_raises("non-retryable raised immediately",
            lambda: retry_call(lambda: (lambda: (_ for _ in ()).throw(IOError(400, "bad")))(), attempts=3,
                               base_delay=0.0,
                               retryable=lambda e: getattr(e, "errno", None) == 429),
            IOError)

# 6. retryable predicate honored on a retryable failure: errno 429 retried
#    exactly once, then success (2 calls)
test("predicate retries retryable once then succeeds",
     lambda: (lambda c=[0]: (
         retry_call((lambda: (c.__setitem__(0, c[0] + 1),
                              (lambda: (_ for _ in ()).throw(IOError(429, "rate")))() if c[0] == 1 else "ok")[1]),
                    attempts=3, base_delay=0.0,
                    retryable=lambda e: getattr(e, "errno", None) == 429),
         c[0])[1] == 2)(),
     True)

# 7. retryable as a type tuple: only the listed type is retried
test("retryable tuple retries listed type only",
     lambda: (lambda c=[0]: (
         retry_call((lambda: (c.__setitem__(0, c[0] + 1),
                              (lambda: (_ for _ in ()).throw(TypeError("x")))() if c[0] < 3 else "ok")[1]),
                    attempts=5, base_delay=0.0, retryable=(TypeError,)),
         c[0])[1] == 3)(),
     True)

# 8. attempts=0 rejected (validation mirrors Node.__post_init__, T8-F4)
test_raises("attempts zero rejected",
            lambda: retry_call(lambda: 1, attempts=0), ValueError)

# 9. attempts=-2 rejected
test_raises("negative attempts rejected",
            lambda: retry_call(lambda: 1, attempts=-2), ValueError)

# 10. jitter path still succeeds (never negative / never breaks the call)
test("jitter does not break success",
     lambda: retry_call(lambda: "ok", attempts=2, base_delay=0.01, jitter=True),
     "ok")

# 11. attempts=1 calls once and returns
test("attempts one calls once",
     lambda: (lambda c=[0]: (
         retry_call((lambda: (c.__setitem__(0, c[0] + 1), "x")[1]),
                    attempts=1, base_delay=0.0),
         c[0])[1] == 1)(),
     True)

# 12. constant backoff (backoff=1.0) succeeds with base_delay each retry
test("constant backoff succeeds",
     lambda: (lambda c=[0]: (
         retry_call((lambda: (c.__setitem__(0, c[0] + 1),
                              "ok" if c[0] >= 2 else (lambda: (_ for _ in ()).throw(IOError("t")))())[1]),
                    attempts=3, base_delay=0.0, backoff=1.0),
         c[0])[1] == 2)(),
     True)

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
