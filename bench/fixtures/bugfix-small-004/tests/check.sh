#!/usr/bin/env bash
# ============================================================================
# Test Suite — bugfix-small-004
# ============================================================================
# Tests that a Python function `merge_unique(left, right)` correctly merges
# two lists preserving first-seen order and removing duplicates.
#
# Usage:
#   ./fixtures/bugfix-small-004/tests/check.sh            # test with bundled broken version
#   ./fixtures/bugfix-small-004/tests/check.sh /path/to/implementation.py   # test candidate fix
#
# Exit code: 0 if all tests pass, 1 if any fail.
# ============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
FIXTURE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# If an implementation file is provided, use it; otherwise use the bundled
# starter (which is deliberately broken).
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
	# Default: deliberately broken starter (sorted(set(...)) — changes order, fails mixed types)
	cat >"$WORKDIR/implementation.py" <<'PYEOF'
def merge_unique(left, right):
    # BUG: sorted(set(...)) changes order and fails on mixed hashable types
    return sorted(set(left + right))
PYEOF
fi

# ── Build test runner ──────────────────────────────────────────────────────
cat >"$WORKDIR/test_runner.py" <<'PYEOF'
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from implementation import merge_unique

tests = []

def test(name, fn, expected):
    try:
        result = fn()
        ok = result == expected
        tests.append((name, ok, result, expected))
    except Exception as e:
        tests.append((name, False, str(e), expected))

# ── Normal cases ────────────────────────────────────────────────────────────

test("simple overlap",
    lambda: merge_unique([1, 2, 3], [2, 3, 4]),
    [1, 2, 3, 4])

test("duplicates within left",
    lambda: merge_unique([1, 1, 2, 3], [4, 5]),
    [1, 2, 3, 4, 5])

test("duplicates within right",
    lambda: merge_unique([1, 2], [2, 2, 3, 3]),
    [1, 2, 3])

test("all unique",
    lambda: merge_unique([1, 2], [3, 4]),
    [1, 2, 3, 4])

test("empty left",
    lambda: merge_unique([], [1, 2, 3]),
    [1, 2, 3])

test("empty right",
    lambda: merge_unique([1, 2, 3], []),
    [1, 2, 3])

test("both empty",
    lambda: merge_unique([], []),
    [])

test("strings preserving order",
    lambda: merge_unique(["hello", "world"], ["world", "python"]),
    ["hello", "world", "python"])

test("mixed hashable types preserving order",
    lambda: merge_unique([1, "a", (1,2)], ["a", 1, (3,)]),
    [1, "a", (1,2), (3,)])

# ── Report ──────────────────────────────────────────────────────────────────
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
