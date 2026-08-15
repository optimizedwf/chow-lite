#!/usr/bin/env bash
# ============================================================================
# Test Suite — bugfix-small-011 (honest claim verification)
# ============================================================================
# verify_claims(claims, evidence_text) must:
#   * one verdict dict per claim, input order preserved, NEVER dropped
#   * present claim  -> VERIFIED with the EXACT matched excerpt as evidence
#     (never the whole corpus — full-text "evidence" is the lie this fixture
#     exists to catch)
#   * absent claim   -> UNVERIFIED with evidence "" (honest, not VERIFIED)
#   * "NOT X" claim  -> FAILED with excerpt when X is present (the audit's
#     teeth), VERIFIED with "" when X is absent (never dropped, never
#     downgraded to UNVERIFIED)
#   * status exactly one of VERIFIED | UNVERIFIED | FAILED; dict keys
#     exactly claim/status/evidence
#
# Usage:
#   ./fixtures/bugfix-small-011/tests/check.sh            # broken starter
#   ./fixtures/bugfix-small-011/tests/check.sh /path/to/implementation.py
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
"""BUGGY STARTER for bugfix-small-011."""
import re

def verify_claims(claims, evidence_text):
    out = []
    for c in claims:
        if c.startswith("NOT "):
            needle = c[4:]
            if re.search(re.escape(needle), evidence_text, re.IGNORECASE):
                out.append({"claim": c, "status": "UNVERIFIED", "evidence": ""})
            else:
                continue  # BUG: drops the claim entirely
        elif re.search(re.escape(c), evidence_text, re.IGNORECASE):
            out.append({"claim": c, "status": "VERIFIED", "evidence": evidence_text})
        else:
            out.append({"claim": c, "status": "VERIFIED", "evidence": ""})
    return out
PYEOF
fi

cat >"$WORKDIR/test_runner.py" <<'PYEOF'
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from implementation import verify_claims

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

# 1. present claim -> VERIFIED with exact excerpt evidence
test("present claim verified with exact excerpt",
     lambda: verify_claims(["server is down"], "The server is down since 3pm."),
     [{"claim": "server is down", "status": "VERIFIED", "evidence": "server is down"}])

# 2. absent claim -> UNVERIFIED with empty evidence (starter stamps VERIFIED)
test("absent claim is honestly UNVERIFIED",
     lambda: verify_claims(["no monkeys"], "The server is down."),
     [{"claim": "no monkeys", "status": "UNVERIFIED", "evidence": ""}])

# 3. negative claim contradicted by evidence -> FAILED with excerpt
#    (starter downgrades to UNVERIFIED)
test("NOT claim present in evidence is FAILED",
     lambda: verify_claims(["NOT alpha"], "alpha is everywhere"),
     [{"claim": "NOT alpha", "status": "FAILED", "evidence": "alpha"}])

# 4. negative claim absent -> VERIFIED with "" (starter DROPS the claim)
test("NOT claim absent from evidence is VERIFIED",
     lambda: verify_claims(["NOT zzz"], "alpha is everywhere"),
     [{"claim": "NOT zzz", "status": "VERIFIED", "evidence": ""}])

# 5. multiple claims: order preserved, none dropped, mixed statuses
test("mixed claims keep order and completeness",
     lambda: verify_claims(["alpha is up", "NOT alpha", "gamma down"],
                           "alpha is up and gamma down."),
     [{"claim": "alpha is up", "status": "VERIFIED", "evidence": "alpha is up"},
      {"claim": "NOT alpha", "status": "FAILED", "evidence": "alpha"},
      {"claim": "gamma down", "status": "VERIFIED", "evidence": "gamma down"}])

# 6. case-insensitive match but excerpt is the ORIGINAL casing from evidence
test("match is case-insensitive, excerpt keeps evidence casing",
     lambda: verify_claims(["SERVER IS DOWN"], "The server is down since 3pm."),
     [{"claim": "SERVER IS DOWN", "status": "VERIFIED", "evidence": "server is down"}])

# 7. empty claims list -> empty output
test("empty claims yield empty verdicts",
     lambda: verify_claims([], "anything"),
     [])

# 8. evidence field must NOT be the whole corpus (starter returns evidence_text)
test("evidence is exact excerpt not whole corpus",
     lambda: verify_claims(["down"], "The server is down since 3pm."),
     [{"claim": "down", "status": "VERIFIED", "evidence": "down"}])

# 9. verdict dict keys are exactly claim/status/evidence
test("verdict shape has exactly three keys",
     lambda: sorted(verify_claims(["x"], "x")[0].keys()),
     ["claim", "evidence", "status"])

# 10. status strings come from the enum only
test("statuses are enum-exact strings",
     lambda: sorted({v["status"] for v in verify_claims(
         ["alpha is up", "NOT alpha", "beta gone"], "alpha is up")}),
     ["FAILED", "UNVERIFIED", "VERIFIED"])

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
