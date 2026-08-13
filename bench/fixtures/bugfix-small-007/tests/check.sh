#!/usr/bin/env bash
# ============================================================================
# Test Suite — bugfix-small-007 (empty-and-whitespace-input)
# ============================================================================
# title_case(text) must never raise and must handle empty strings,
# whitespace-only strings, newlines, and non-ASCII unicode correctly.
#
# Usage:
#   ./fixtures/bugfix-small-007/tests/check.sh            # broken starter
#   ./fixtures/bugfix-small-007/tests/check.sh /path/to/implementation.py
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
def title_case(text):
    # BUG: crashes with IndexError on the empty string
    return text[0].upper() + text[1:].lower()
PYEOF
fi

cat >"$WORKDIR/test_runner.py" <<'PYEOF'
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from implementation import title_case

tests = []

def test(name, fn, expected):
    try:
        result = fn()
        ok = result == expected
        tests.append((name, ok, result, expected))
    except Exception as e:
        tests.append((name, False, str(e), expected))

# Normal cases
test("ascii phrase",
    lambda: title_case("hello world"),
    "Hello world")
test("already capital",
    lambda: title_case("Hello"),
    "Hello")
test("rest lowercased",
    lambda: title_case("hELLO"),
    "Hello")

# Empty / whitespace must NEVER raise
test("empty string",
    lambda: title_case(""),
    "")
test("whitespace only",
    lambda: title_case("   "),
    "   ")
test("newline only",
    lambda: title_case("\n"),
    "\n")
test("tab only",
    lambda: title_case("\t"),
    "\t")

# Unicode safety
test("unicode accents",
    lambda: title_case("héllo"),
    "Héllo")
test("unicode CJK",
    lambda: title_case("テスト"),
    "テスト")
test("unicode symbol",
    lambda: title_case("✓ ok"),
    "✓ ok")

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
