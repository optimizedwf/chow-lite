"""Hermetic regression tests for bench fixtures bugfix-small-006/007/008
(T4-F7 — strict-JSON output, empty/unicode input, missing-env fail-loud).

Each fixture must: keep its layout contract, FAIL against the bundled broken
starter, PASS against a corrected candidate, and convert cleanly into pytest
for the debug lane (bench_nine.py extract_runner + convert_to_pytest).
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO / "bench" / "fixtures"

NEW_FIXTURES = [f"bugfix-small-{n:03d}" for n in (6, 7, 8)]

FIXED = {
    "bugfix-small-006": (
        "import json\n"
        "\n"
        "def _strict_loads(s):\n"
        "    def _reject(_token):\n"
        "        raise ValueError(\"non-standard JSON constant\")\n"
        "    return json.loads(s, parse_constant=_reject)\n"
        "\n"
        "def render_eval_json(checks):\n"
        "    out = []\n"
        "    for c in checks:\n"
        "        out.append({\"name\": c.get(\"name\", \"\"),\n"
        "                   \"passed\": c.get(\"passed\", False),\n"
        "                   \"message\": c.get(\"message\", \"\")})\n"
        "    return json.dumps({\"checks\": out})\n"
        "\n"
        "def validate_eval_json(s):\n"
        "    try:\n"
        "        data = _strict_loads(s)\n"
        "    except Exception:\n"
        "        return False\n"
        "    if not isinstance(data, dict):\n"
        "        return False\n"
        "    checks = data.get(\"checks\")\n"
        "    if not isinstance(checks, list):\n"
        "        return False\n"
        "    for c in checks:\n"
        "        if not isinstance(c, dict):\n"
        "            return False\n"
        "        if \"name\" not in c or not isinstance(c[\"name\"], str):\n"
        "            return False\n"
        "        if \"passed\" not in c or not isinstance(c[\"passed\"], bool):\n"
        "            return False\n"
        "        if \"message\" not in c or not isinstance(c[\"message\"], str):\n"
        "            return False\n"
        "    return True\n"
    ),
    "bugfix-small-007": "def title_case(text):\n    return text[:1].upper() + text[1:].lower()\n",
    "bugfix-small-008": (
        "import os\n"
        "import sys\n"
        "\n"
        "def check_token(value):\n"
        "    return \"ok\" if value and value.strip() else \"missing\"\n"
        "\n"
        "def main():\n"
        "    token = os.environ.get(\"NINE_TEST_TOKEN\", \"\")\n"
        "    if check_token(token) != \"ok\":\n"
        "        sys.stderr.write(\"[error] NINE_TEST_TOKEN missing or whitespace\\n\")\n"
        "        return 1\n"
        "    sys.stdout.write(f\"[ok] token accepted: {token.strip()[:4]}...\\n\")\n"
        "    return 0\n"
        "\n"
        "if __name__ == \"__main__\":\n"
        "    sys.exit(main())\n"
    ),
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
                       check=False,
                       capture_output=True, text=True)
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
    """The debug lane's convert path (bench_nine) must work for the new fixtures."""
    from bench.bench_nine import convert_to_pytest, extract_runner

    check_sh = FIXTURES_DIR / fx / "tests" / "check.sh"
    pytest_src = convert_to_pytest(extract_runner(check_sh))
    assert "\ndef test_" in pytest_src
    (tmp_path / "solution.py").write_text(FIXED[fx], encoding="utf-8")
    (tmp_path / "test_solution.py").write_text(pytest_src, encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "pytest", "test_solution.py", "-q"],
                       cwd=tmp_path, check=False, capture_output=True, text=True)
    assert r.returncode == 0, f"{fx}: converted pytest failed:\n{r.stdout[-500:]}{r.stderr[-300:]}"


def test_bench_nine_defaults_include_new_fixtures():
    from bench.bench_nine import FIXTURES

    for fx in NEW_FIXTURES:
        assert fx in FIXTURES, f"{fx} missing from bench_nine default FIXTURES"


def test_fixture_008_cli_fail_loud_is_hermetic(tmp_path):
    """Direct CLI contract check independent of check.sh: unset env must exit
    1 with an [error] line on stderr and NO traceback; valid env exits 0."""
    impl = tmp_path / "impl.py"
    impl.write_text(FIXED["bugfix-small-008"], encoding="utf-8")
    import os as _os

    env = dict(_os.environ)
    env.pop("NINE_TEST_TOKEN", None)
    r = subprocess.run([sys.executable, "impl.py"], cwd=tmp_path, env=env, check=False,
                       capture_output=True, text=True)
    assert r.returncode == 1
    assert "[error]" in r.stderr
    assert "traceback" not in r.stderr.lower()

    env["NINE_TEST_TOKEN"] = "   "
    r = subprocess.run([sys.executable, "impl.py"], cwd=tmp_path, env=env, check=False,
                       capture_output=True, text=True)
    assert r.returncode == 1

    env["NINE_TEST_TOKEN"] = "sk-test-123"
    r = subprocess.run([sys.executable, "impl.py"], cwd=tmp_path, env=env, check=False,
                       capture_output=True, text=True)
    assert r.returncode == 0
    assert "[ok]" in r.stdout
