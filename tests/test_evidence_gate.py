"""Hermetic armor for the evidence gate (slice 24 HARDEN).

The gate is the honesty boundary: it decides SHIP/FIX from EVAL.json. Two
classes of bug were possible:

1. TRUTHINESS LIE: `"passed": "false"` (a string, written by a buggy node)
   is truthy in Python, so `not c.get("passed", False)` treated it as
   PASSED -> the gate SHIPped work whose own EVAL.json said FAILED.
2. SHAPE CRASHES: a non-dict EVAL.json root, non-object check entries, or a
   failed check without a `name` raised AttributeError/KeyError inside the
   check (degraded to an ugly "check error: ..." instead of a real reason).

The contract is now: only literal JSON `true` passes a check; anything else
(false, "false", "true", 1, 0, null, missing) FAILS; malformed shapes fail
closed with a clear message. Never SHIP on a string.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

import os

os.environ["GEMINI_API_KEY"] = ""

import json

import pytest

from nine.gates.evidence import (
    EvidenceGate,
    eval_json_check,
    exit_codes_check,
    file_nonempty_check,
    load_eval_json,
    required_artifact_check,
)


def _eval_result(workdir, evjson, expected=("build-tests",)):
    """Write EVAL.json and run eval_json_check against it."""
    (workdir / "EVAL.json").write_text(evjson, encoding="utf-8")
    g = EvidenceGate()
    g.register_check("eval", eval_json_check(list(expected)))
    return g.evaluate({}, workdir)["eval_results"]["eval"]


# ---------------------------------------------------------------- strict bool


@pytest.mark.parametrize(
    "passed_value",
    ["false", "true", 1, 0, None],  # JSON literals, not Python bools
)
def test_non_boolean_passed_never_ships(tmp_path, passed_value):
    """A check that did NOT write literal `true` must never pass."""
    payload = json.dumps(
        {"checks": [{"name": "build-tests", "passed": passed_value, "message": "m"}]}
    )
    res = _eval_result(tmp_path, payload)
    assert res["passed"] is False
    assert "failed" in res["message"]


def test_string_false_is_a_failure(tmp_path):
    """THE lie: 'passed':"false" used to SHIP (truthy string)."""
    res = _eval_result(
        tmp_path,
        '{"checks":[{"name":"build-tests","passed":"false","message":"tests FAILED"}]}',
    )
    assert res["passed"] is False
    assert "build-tests" in res["message"]


def test_missing_passed_fails(tmp_path):
    """A check with no `passed` key is not evidence of success."""
    res = _eval_result(tmp_path, '{"checks":[{"name":"build-tests","message":"m"}]}')
    assert res["passed"] is False


def test_literal_true_ships(tmp_path):
    """Happy path: real JSON true + expected check present -> SHIP."""
    res = _eval_result(
        tmp_path,
        '{"checks":[{"name":"build-tests","passed":true,"message":"ok"}]}',
    )
    assert res["passed"] is True
    assert "1 checks passed" in res["message"]


def test_expected_check_missing_fails(tmp_path):
    res = _eval_result(
        tmp_path,
        '{"checks":[{"name":"other","passed":true,"message":"ok"}]}',
    )
    assert res["passed"] is False
    assert "expected checks" in res["message"]


# ------------------------------------------------------------ shape defenses


def test_list_root_is_not_eval(tmp_path):
    res = _eval_result(tmp_path, '[{"name":"build-tests","passed":true}]')
    assert res["passed"] is False
    assert "JSON object" in res["message"]


def test_scalar_root_is_not_eval(tmp_path):
    for payload in ("42", '"hello"', "true"):
        res = _eval_result(tmp_path, payload)
        assert res["passed"] is False
        assert "JSON object" in res["message"]


def test_non_object_check_entries_fail_closed(tmp_path):
    res = _eval_result(tmp_path, '{"checks":["build-tests", 7]}')
    assert res["passed"] is False
    assert "bad entry" in res["message"]


def test_failed_check_without_name_does_not_keyerror(tmp_path):
    res = _eval_result(tmp_path, '{"checks":[{"passed":false,"message":"boom"}]}',
                     expected=())
    assert res["passed"] is False
    assert "<unnamed>" in res["message"]


def test_partial_json_fails_closed(tmp_path):
    res = _eval_result(tmp_path, '{"checks":[{"name":"x","passed":tr')
    assert res["passed"] is False
    assert "not valid JSON" in res["message"]


def test_missing_eval_json_fails_closed(tmp_path):
    g = EvidenceGate()
    g.register_check("eval", eval_json_check(["build-tests"]))
    res = g.evaluate({}, tmp_path)["eval_results"]["eval"]
    assert res["passed"] is False
    assert "missing" in res["message"]


def test_load_eval_json_returns_error_for_non_dict(tmp_path):
    (tmp_path / "EVAL.json").write_text("[1,2,3]", encoding="utf-8")
    assert load_eval_json(tmp_path)["error"]


# ------------------------------------------------------------ other factories


def test_file_nonempty_check(tmp_path):
    check = file_nonempty_check("RESPONSE.md", min_chars=5)
    # missing
    assert check({}, tmp_path)[0] is False
    # too small
    (tmp_path / "RESPONSE.md").write_text("hi", encoding="utf-8")
    assert check({}, tmp_path)[0] is False
    # ok
    (tmp_path / "RESPONSE.md").write_text("a real answer", encoding="utf-8")
    assert check({}, tmp_path)[0] is True


def test_required_artifact_check(tmp_path):
    check = required_artifact_check(["EVAL.json", "solution.py"])
    assert check({}, tmp_path)[0] is False
    (tmp_path / "EVAL.json").write_text("{}", encoding="utf-8")
    assert check({}, tmp_path)[0] is False  # solution.py still missing
    (tmp_path / "solution.py").write_text("x = 1", encoding="utf-8")
    assert check({}, tmp_path)[0] is True


def test_exit_codes_check(tmp_path):
    check = exit_codes_check()
    # no bash nodes -> vacuously pass
    assert check({}, tmp_path)[0] is True
    assert check({"node_exit_codes": {}}, tmp_path)[0] is True
    assert check({"node_exit_codes": {"a": 0, "b": 0}}, tmp_path)[0] is True
    ok, msg = check({"node_exit_codes": {"a": 0, "b": 2}}, tmp_path)
    assert ok is False
    assert "2" in msg
