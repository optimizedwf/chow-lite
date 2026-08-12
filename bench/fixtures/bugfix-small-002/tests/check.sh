#!/usr/bin/env bash
# ============================================================================
# Test Suite — bugfix-small-002
# ============================================================================
# Tests that a Python function `normalize_email(email)` correctly trims
# whitespace, lowercases only the domain part, preserves local-part case,
# and validates exactly one '@'.
#
# Usage:
#   ./fixtures/bugfix-small-002/tests/check.sh            # test with bundled broken version
#   ./fixtures/bugfix-small-002/tests/check.sh /path/to/implementation.py   # test candidate fix
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
	# Default: deliberately broken starter (lowercases everything, no validation)
	cat >"$WORKDIR/implementation.py" <<'PYEOF'
def normalize_email(email):
    # BUG: lowercases entire email and doesn't validate exactly one '@'
    return email.strip().lower()
PYEOF
fi

# ── Build test runner ──────────────────────────────────────────────────────
cat >"$WORKDIR/test_runner.py" <<'PYEOF'
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from implementation import normalize_email

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

test("whitespace trimmed, domain lowercased, local preserved",
    lambda: normalize_email("  User@Example.COM  "),
    "User@example.com")

test("local part preserved when uppercase",
    lambda: normalize_email("USER@EXAMPLE.COM"),
    "USER@example.com")

test("local part with dot preserved",
    lambda: normalize_email("John.Doe@Example.com"),
    "John.Doe@example.com")

test("already normalized passes through",
    lambda: normalize_email("user@example.com"),
    "user@example.com")

test("subdomain handling",
    lambda: normalize_email("user@Sub.Example.COM"),
    "user@sub.example.com")

# ── Validation / error cases ────────────────────────────────

test_raises("missing @ raises ValueError",
    lambda: normalize_email("missing-at"))

test_raises("multiple @ raises ValueError",
    lambda: normalize_email("a@b@c.com"))

test_raises("empty string raises ValueError",
    lambda: normalize_email(""))

test_raises("whitespace-only string raises ValueError",
    lambda: normalize_email("   "))

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
