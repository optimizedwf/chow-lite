#!/usr/bin/env bash
# ============================================================================
# Test Suite — bugfix-small-006 (strict-json-output)
# ============================================================================
# render_eval_json(checks) must emit STRICT JSON (real booleans, no NaN, no
# trailing commas, no missing keys); validate_eval_json(s) must accept ONLY
# the strict shape and reject "true"/"false" strings, 1/0, null, NaN, missing
# keys, trailing commas, non-list checks.
#
# Usage:
#   ./fixtures/bugfix-small-006/tests/check.sh            # broken starter
#   ./fixtures/bugfix-small-006/tests/check.sh /path/to/implementation.py
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
import json

def render_eval_json(checks):
    parts = []
    for c in checks:
        parts.append('    {"name": "%s", "passed": "%s", "message": "%s"}' % (
            c.get("name", ""), str(c.get("passed", False)).lower(), c.get("message", "")))
    return '{\n  "checks": [\n' + ",\n".join(parts) + '\n  ]\n}'

def validate_eval_json(s):
    try:
        data = json.loads(s)
    except Exception:
        return False
    checks = data.get("checks", [])
    if not isinstance(checks, list):
        return False
    for c in checks:
        if c.get("passed") not in (True, False, "true", "false", 1, 0):
            return False
    return True
PYEOF
fi

cat >"$WORKDIR/test_runner.py" <<'PYEOF'
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from implementation import render_eval_json, validate_eval_json

tests = []

def test(name, fn, expected):
    try:
        result = fn()
        ok = result == expected
        tests.append((name, ok, result, expected))
    except Exception as e:
        tests.append((name, False, str(e), expected))

test("valid true round-trip",
    lambda: validate_eval_json(render_eval_json([{"name": "t", "passed": True}])),
    True)
test("valid false round-trip",
    lambda: validate_eval_json(render_eval_json([{"name": "t", "passed": False}])),
    True)
test("valid multi-check round-trip",
    lambda: validate_eval_json(render_eval_json([
        {"name": "a", "passed": True, "message": "ok"},
        {"name": "b", "passed": False}])),
    True)
test("rejects string true",
    lambda: validate_eval_json('{"checks":[{"name":"t","passed":"true"}]}'),
    False)
test("rejects string false",
    lambda: validate_eval_json('{"checks":[{"name":"t","passed":"false"}]}'),
    False)
test("rejects int one",
    lambda: validate_eval_json('{"checks":[{"name":"t","passed":1}]}'),
    False)
test("rejects int zero",
    lambda: validate_eval_json('{"checks":[{"name":"t","passed":0}]}'),
    False)
test("rejects null",
    lambda: validate_eval_json('{"checks":[{"name":"t","passed":null}]}'),
    False)
test("rejects missing passed",
    lambda: validate_eval_json('{"checks":[{"name":"t"}]}'),
    False)
test("rejects NaN",
    lambda: validate_eval_json('{"checks":[{"name":"t","passed":NaN}]}'),
    False)
test("rejects trailing comma",
    lambda: validate_eval_json('{"checks":[{"name":"t","passed":true},]}'),
    False)
test("rejects non-list checks",
    lambda: validate_eval_json('{"checks":{}}'),
    False)

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
