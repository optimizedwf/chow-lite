"""Hermetic regression tests for bench fixture bugfix-small-011 (honest claim
verification — the verify-lane honesty doctrine in fixture form: a verifier
that lies, drops a claim, or invents evidence is the cardinal sin; an honest
UNVERIFIED still ships).

The fixture must: keep its layout contract, FAIL against the bundled broken
starter (negative control), PASS against a corrected candidate, and convert
cleanly into pytest for the debug lane (extract_runner + convert_to_pytest).
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO / "bench" / "fixtures"

NEW_FIXTURES = ["bugfix-small-011"]

FIXED = {
    "bugfix-small-011": 'import re\n\ndef verify_claims(claims, evidence_text):\n    out = []\n    for c in claims:\n        negated = c.startswith("NOT ")\n        needle = c[4:] if negated else c\n        m = re.search(re.escape(needle), evidence_text, re.IGNORECASE)\n        if negated:\n            if m:\n                out.append({"claim": c, "status": "FAILED", "evidence": m.group(0)})\n            else:\n                out.append({"claim": c, "status": "VERIFIED", "evidence": ""})\n        else:\n            if m:\n                out.append({"claim": c, "status": "VERIFIED", "evidence": m.group(0)})\n            else:\n                out.append({"claim": c, "status": "UNVERIFIED", "evidence": ""})\n    return out\n',
}


@pytest.mark.parametrize("fx", NEW_FIXTURES)
def test_fixture_layout_contract(fx):
    d = FIXTURES_DIR / fx
    for rel in ("task.md", "expected-behavior.md", "rubric.json",
                "starter/solution.py", "tests/check.sh"):
        assert (d / rel).is_file(), f"{fx}: missing {rel}"
    rubric = json.loads((d / "rubric.json").read_text())
    assert rubric["fixture_id"] == fx
    assert rubric["verdict_thresholds"]["pass"] > 0
    assert (d / "tests" / "check.sh").stat().st_mode & 0o111, f"{fx}: check.sh not executable"

@pytest.mark.parametrize("fx", NEW_FIXTURES)
def test_starter_is_broken(fx):
    """The bundled starter must FAIL the fixture (negative control)."""
    r = subprocess.run(["bash", str(FIXTURES_DIR / fx / "tests" / "check.sh")],
                       check=False, capture_output=True, text=True)
    assert r.returncode != 0, f"{fx}: starter unexpectedly passed"

@pytest.mark.parametrize("fx", NEW_FIXTURES)
def test_fixed_candidate_passes(fx, tmp_path):
    cand = tmp_path / "implementation.py"
    cand.write_text(FIXED[fx], encoding="utf-8")
    r = subprocess.run(["bash", str(FIXTURES_DIR / fx / "tests" / "check.sh"), str(cand)],
                       check=False, capture_output=True, text=True)
    assert r.returncode == 0, f"{fx}: fixed candidate failed:\n{r.stdout[-800:]}"

@pytest.mark.parametrize("fx", NEW_FIXTURES)
def test_checksh_converts_to_pytest_and_passes(fx, tmp_path):
    """The debug lane's convert path (bench_nine) must work for the new fixture."""
    from bench.bench_nine import convert_to_pytest, extract_runner

    check_sh = FIXTURES_DIR / fx / "tests" / "check.sh"
    pytest_src = convert_to_pytest(extract_runner(check_sh))
    assert "\ndef test_" in pytest_src
    (tmp_path / "solution.py").write_text(FIXED[fx], encoding="utf-8")
    (tmp_path / "test_solution.py").write_text(pytest_src, encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "pytest", "test_solution.py", "-q"],
                       cwd=tmp_path, check=False, capture_output=True, text=True)
    assert r.returncode == 0, f"{fx}: converted pytest failed:\n{r.stdout[-500:]}{r.stderr[-300:]}"

def test_bench_nine_defaults_include_new_fixture():
    from bench.bench_nine import FIXTURES

    assert "bugfix-small-011" in FIXTURES, "bugfix-small-011 missing from bench_nine default FIXTURES"

def test_fixture_011_honesty_contract_is_hermetic(tmp_path):
    """Direct contract check independent of check.sh: the honest-audit
    invariants (exact excerpt evidence, honest UNVERIFIED, FAILED on
    contradicted negative claim, completeness/order, strict shape)."""
    impl = tmp_path / "impl.py"
    impl.write_text(FIXED["bugfix-small-011"], encoding="utf-8")

    runner = "import sys\nsys.path.insert(0, {td!r})\nfrom impl import verify_claims\nchecks = [\n    verify_claims(['server is down'], 'The server is down since 3pm.')\n        == [{'claim': 'server is down', 'status': 'VERIFIED',\n             'evidence': 'server is down'}],\n    verify_claims(['no monkeys'], 'The server is down.')\n        == [{'claim': 'no monkeys', 'status': 'UNVERIFIED', 'evidence': ''}],\n    verify_claims(['NOT alpha'], 'alpha is everywhere')\n        == [{'claim': 'NOT alpha', 'status': 'FAILED', 'evidence': 'alpha'}],\n    verify_claims(['NOT zzz'], 'alpha is everywhere')\n        == [{'claim': 'NOT zzz', 'status': 'VERIFIED', 'evidence': ''}],\n    verify_claims(['a', 'NOT b', 'c'], 'a and c')\n        == [{'claim': 'a', 'status': 'VERIFIED', 'evidence': 'a'},\n             {'claim': 'NOT b', 'status': 'VERIFIED', 'evidence': ''},\n             {'claim': 'c', 'status': 'VERIFIED', 'evidence': 'c'}],\n    len(verify_claims(['x', 'y', 'z'], '')) == 3,\n]\nsys.exit(0 if all(checks) else 1)\n"
    code = runner.replace("{td!r}", repr(str(tmp_path)))
    r = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, check=False,
                       capture_output=True, text=True)
    assert r.returncode == 0, f"011 honesty contract failed:\n{r.stdout}{r.stderr}"
