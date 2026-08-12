#!/usr/bin/env bash
# ============================================================================
# Test Suite — bugfix-small-003
# ============================================================================
# Tests that a Python function `parse_int_list(text)` correctly parses
# comma-separated integers, trims whitespace, ignores empty segments,
# and raises ValueError on invalid integer tokens.
#
# Usage:
#   ./fixtures/bugfix-small-003/tests/check.sh            # test with bundled broken version
#   ./fixtures/bugfix-small-003/tests/check.sh /path/to/implementation.py   # test candidate fix
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
	# Default: deliberately broken starter (no whitespace/empty/invalid handling)
	cat >"$WORKDIR/implementation.py" <<'PYEOF'
def parse_int_list(text):
    # BUG: doesn't handle whitespace, empty segments, or invalid tokens
    return [int(x) for x in text.split(',')]
PYEOF
fi

# ── Build test runner ──────────────────────────────────────────────────────
cat >"$WORKDIR/test_runner.py" <<'PYEOF'
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from implementation import parse_int_list

tests = []

def test(name, fn, expected):
    try:
        result = fn()
        ok = result == expected
        tests.append((name, ok, result, expected))
    except Exception as e:
        tests.append((name, False, str(e), expected))

def test_raises(name, fn, exc_type=ValueError):
    try:
        result = fn()
        tests.append((name, False, f"no exception raised, got {result!r}", exc_type.__name__))
    except exc_type:
        tests.append((name, True, exc_type.__name__, exc_type.__name__))
    except Exception as e:
        tests.append((name, False, f"wrong exception: {type(e).__name__}: {e}", exc_type.__name__))

# ── Normal cases ────────────────────────────────────────────────────────────

test("simple comma-separated integers",
    lambda: parse_int_list("1,2,3"),
    [1, 2, 3])

test("whitespace around tokens",
    lambda: parse_int_list(" 1, 2 , 3 "),
    [1, 2, 3])

test("negative numbers",
    lambda: parse_int_list("-1,0,42"),
    [-1, 0, 42])

test("repeated commas (empty segment)",
    lambda: parse_int_list("1,,2,3"),
    [1, 2, 3])

test("trailing comma",
    lambda: parse_int_list("1,2,"),
    [1, 2])

# ── Edge cases ──────────────────────────────────────────────────────────────

test("empty string returns empty list",
    lambda: parse_int_list(""),
    [])

test("whitespace-only returns empty list",
    lambda: parse_int_list("   "),
    [])

# ── Error cases ─────────────────────────────────────────────────────────────

test_raises("invalid token raises ValueError",
    lambda: parse_int_list("1,a,3"))

test_raises("mixed valid/invalid raises ValueError",
    lambda: parse_int_list("1,2,three"))

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
