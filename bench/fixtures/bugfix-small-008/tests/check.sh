#!/usr/bin/env bash
# ============================================================================
# Test Suite — bugfix-small-008 (missing-env-degradation)
# ============================================================================
# check_token(value) accepts only non-empty non-whitespace tokens; main()
# must FAIL LOUD when NINE_TEST_TOKEN is unset/empty/whitespace: exit 1 with
# exactly one [error] line on stderr and NO traceback; valid token -> exit 0
# with an [ok] line on stdout.
#
# Usage:
#   ./fixtures/bugfix-small-008/tests/check.sh            # broken starter
#   ./fixtures/bugfix-small-008/tests/check.sh /path/to/implementation.py
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
import os
import sys

def check_token(value):
    # BUG: whitespace is truthy, so it is treated as a valid token
    return "ok" if value else "missing"

def main():
    # BUG: unset env -> KeyError traceback instead of a clean error
    token = os.environ["NINE_TEST_TOKEN"]
    if not token:
        sys.stderr.write("[error] NINE_TEST_TOKEN is empty\n")
        return 1
    sys.stdout.write(f"[ok] token accepted: {token[:4]}...\n")
    return 0

if __name__ == "__main__":
    sys.exit(main())
PYEOF
fi

# ── Pure check_token tests (pytest-convertible for the debug lane) ──────────
cat >"$WORKDIR/test_runner.py" <<'PYEOF'
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from implementation import check_token

tests = []

def test(name, fn, expected):
    try:
        result = fn()
        ok = result == expected
        tests.append((name, ok, result, expected))
    except Exception as e:
        tests.append((name, False, str(e), expected))

test("valid token",
    lambda: check_token("sk-test-123"),
    "ok")
test("empty token",
    lambda: check_token(""),
    "missing")
test("whitespace token",
    lambda: check_token("   "),
    "missing")
test("tab token",
    lambda: check_token("\t"),
    "missing")
test("none token",
    lambda: check_token(None),
    "missing")

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

echo "  ── Running pure check_token tests ─────────────────────────"
cd "$WORKDIR"
python3 test_runner.py
RUNNER_CODE=$?
echo "  ────────────────────────────────────────────────────────────"

# ── CLI fail-loud tests (bash-only, not pytest-convertible) ────────────────
echo "  ── Running CLI fail-loud tests ────────────────────────────"
cli_fail=0

run_cli() {
	# $1 = "UNSET" | literal value to set
	if [ "$1" = "UNSET" ]; then
		(cd "$WORKDIR" && env -u NINE_TEST_TOKEN python3 implementation.py) >"$WORKDIR/cli.out" 2>"$WORKDIR/cli.err"
	else
		(cd "$WORKDIR" && NINE_TEST_TOKEN="$1" python3 implementation.py) >"$WORKDIR/cli.out" 2>"$WORKDIR/cli.err"
	fi
	echo $?
}

CODE=$(run_cli UNSET)
if [ "$CODE" -ne 1 ]; then
	fail "unset env must exit 1 (got $CODE)"
	cli_fail=1
fi
if ! grep -q '\[error\]' "$WORKDIR/cli.err"; then
	fail "unset env must print an [error] line on stderr"
	cli_fail=1
fi
if grep -qi 'traceback' "$WORKDIR/cli.err"; then
	fail "unset env must NOT print a traceback"
	cli_fail=1
fi

CODE=$(run_cli "")
if [ "$CODE" -ne 1 ]; then
	fail "empty env must exit 1 (got $CODE)"
	cli_fail=1
fi

CODE=$(run_cli "   ")
if [ "$CODE" -ne 1 ]; then
	fail "whitespace env must exit 1 (got $CODE)"
	cli_fail=1
fi
if ! grep -q '\[error\]' "$WORKDIR/cli.err"; then
	fail "whitespace env must print an [error] line on stderr"
	cli_fail=1
fi

CODE=$(run_cli "sk-test-123")
if [ "$CODE" -ne 0 ]; then
	fail "valid env must exit 0 (got $CODE)"
	cli_fail=1
fi
if ! grep -q '\[ok\]' "$WORKDIR/cli.out"; then
	fail "valid env must print an [ok] line on stdout"
	cli_fail=1
fi
echo "  ────────────────────────────────────────────────────────────"

if [ "$RUNNER_CODE" -ne 0 ] || [ "$cli_fail" -ne 0 ]; then
	echo "  ❌ Some tests failed"
	exit 1
fi
echo "  ✅ All tests passed"
exit 0
