#!/usr/bin/env bash
# ============================================================================
# Test Suite — bugfix-small-001
# ============================================================================
# Tests that a Python function `slice_list(items, start, end)` correctly
# returns items from index `start` to `end` inclusive.
#
# Usage:
#   ./fixtures/bugfix-small-001/tests/check.sh            # test with bundled broken version
#   ./fixtures/bugfix-small-001/tests/check.sh /path/to/implementation.py   # test candidate fix
#
# Exit code: 0 if all tests pass, 1 if any fail.
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FIXTURE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# If an implementation file is provided, use it; otherwise use a bundled
# copy that we can patch on the fly.
CANDIDATE="${1:-}"

PASS=0
FAIL=0

pass() { PASS=$((PASS + 1)); }
fail() {
	FAIL=$((FAIL + 1))
	echo "  ❌ $1"
}

# ── Build a temporary test harness ──────────────────────────────────────────
WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

if [ -n "$CANDIDATE" ]; then
	cp "$CANDIDATE" "$WORKDIR/implementation.py"
else
	# Default: a deliberately broken implementation (off-by-one) for negative testing
	cat >"$WORKDIR/implementation.py" <<'PYEOF'
def slice_list(items, start, end):
    # BUG: off-by-one — should be items[start:end+1]
    return items[start:end]
PYEOF
fi

# ── Build test runner ──────────────────────────────────────────────────────
cat >"$WORKDIR/test_runner.py" <<'PYEOF'
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from implementation import slice_list

tests = []

def test(name, func, expected):
    try:
        result = func
        ok = result == expected
        tests.append((name, ok, result, expected))
    except Exception as e:
        tests.append((name, False, str(e), expected))

# Basic correctness
test("basic inclusive slice (1,3)",
    slice_list([10, 20, 30, 40, 50], 1, 3),
    [20, 30, 40])

test("full slice (0,2)",
    slice_list([10, 20, 30, 40, 50], 0, 2),
    [10, 20, 30])

test("end-of-list slice (3,4)",
    slice_list([10, 20, 30, 40, 50], 3, 4),
    [40, 50])

test("single element (0,0)",
    slice_list([10, 20, 30, 40, 50], 0, 0),
    [10])

# Edge cases
test("end beyond length",
    slice_list([10, 20, 30, 40, 50], 1, 10),
    [20, 30, 40, 50])

test("empty list",
    slice_list([], 0, 0),
    [])

test("negative start",
    slice_list([10, 20, 30], -1, 1),
    [10, 20])

test("start > end",
    slice_list([10, 20, 30], 2, 1),
    [])

test("start == end single element",
    slice_list([10, 20, 30], 1, 1),
    [20])

# Report
failed = 0
for name, ok, result, expected in tests:
    status = "PASS" if ok else "FAIL"
    if not ok:
        failed += 1
        print(f"  ❌ {status} — {name}")
        print(f"       got:      {result!r}")
        print(f"       expected: {expected!r}")
    else:
        print(f"  ✅ {status} — {name}")

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
