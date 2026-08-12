#!/usr/bin/env bash
# ============================================================================
# Test Suite — bugfix-small-005
# ============================================================================
# Tests that a Python function `count_words(text)` correctly returns a
# case-insensitive word count dict, stripping simple punctuation at word
# edges and ignoring empty tokens.
#
# Usage:
#   ./fixtures/bugfix-small-005/tests/check.sh            # test with bundled broken version
#   ./fixtures/bugfix-small-005/tests/check.sh /path/to/implementation.py   # test candidate fix
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
	# Default: deliberately broken starter (no case-insensitivity or punctuation stripping)
	cat >"$WORKDIR/implementation.py" <<'PYEOF'
from collections import Counter

def count_words(text):
    # BUG: doesn't handle case-insensitivity or punctuation stripping
    return dict(Counter(text.split()))
PYEOF
fi

# ── Build test runner ──────────────────────────────────────────────────────
cat >"$WORKDIR/test_runner.py" <<'PYEOF'
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from implementation import count_words

tests = []

def test(name, fn, expected):
    try:
        result = fn()
        ok = result == expected
        tests.append((name, ok, result, expected))
    except Exception as e:
        tests.append((name, False, str(e), expected))

# ── Normal cases ────────────────────────────────────────────────────────────

test("simple words",
    lambda: count_words("hello world foo"),
    {"hello": 1, "world": 1, "foo": 1})

test("case-insensitive duplicates",
    lambda: count_words("Hello hello HELLO"),
    {"hello": 3})

test("punctuation edges",
    lambda: count_words("hello! world?"),
    {"hello": 1, "world": 1})

test("mixed punctuation",
    lambda: count_words("hello!!! ...world..."),
    {"hello": 1, "world": 1})

test("repeated whitespace and newlines",
    lambda: count_words("hello   world\n\n\nfoo"),
    {"hello": 1, "world": 1, "foo": 1})

# ── Edge cases ──────────────────────────────────────────────────────────────

test("empty string",
    lambda: count_words(""),
    {})

test("punctuation-only",
    lambda: count_words("!!! ??? ..."),
    {})

test("numbers as tokens",
    lambda: count_words("123 456 123"),
    {"123": 2, "456": 1})

test("apostrophe internal handling",
    lambda: count_words("don't can't don't"),
    {"don't": 2, "can't": 1})

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
