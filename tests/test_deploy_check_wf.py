"""deploy-check workflow tests - hermetic, model-or-fail.

Tests inject fake prompt nodes (risk + decision) via monkeypatch; without
GEMINI_API_KEY the real prompt nodes fail loud (WorkflowError).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

os.environ["GEMINI_API_KEY"] = ""

from nine.gates.evidence import (
    EvidenceGate,
)
from nine.ledger.ledger import JSONLLedger
from nine.runtime.workflows import Node, WorkflowError, WorkflowExecutor
from nine.workflows.deploy_check_wf import deploy_check_hop

GOOD = "def add(a, b):\n    return a + b\n\ndef main():\n    print(add(2, 3))\n\nif __name__ == '__main__':\n    main()\n"
GOOD_TEST = (
    "import os\n"
    "def test_add():\n"
    "    assert add(2, 3) == 5\n"
)
GOOD_SOLUTION = (
    "import os\n"
    "API_KEY = os.environ.get('API_KEY')\n"
    "def add(a, b):\n    return a + b\n"
    "def main():\n    print(add(2, 3))\n"
    "if __name__ == '__main__':\n    main()\n"
)


def _install_fake_prompts(monkeypatch, decision="GO", flaky=False):
    """Replace risk + decision prompt nodes with hermetic ones."""
    from nine.workflows import deploy_check_wf

    state = {"risk_calls": 0, "decision_calls": 0}

    def fake_risk(inputs, job_dir):
        job_dir = Path(job_dir)
        state["risk_calls"] += 1
        (job_dir / "RISK.md").write_text(
            "# RISK\n## Top Risks\n1. [MED] env vars may be missing - "
            "documented in ENV_SCAN.\n## Risk Summary\nLow overall.\n",
            encoding="utf-8")
        return {"output": "wrote RISK.md"}

    monkeypatch.setattr(
        deploy_check_wf, "_risk_prompt_node",
        lambda: Node(id="risk", kind="prompt", run=fake_risk,
                     description="fake risk (hermetic)"))

    def fake_decision(inputs, job_dir):
        job_dir = Path(job_dir)
        state["decision_calls"] += 1
        if flaky and state["decision_calls"] == 1:
            (job_dir / "DEPLOY_CHECK.md").write_text(
                "# DEPLOY CHECK\nNo decision yet.\n", encoding="utf-8")
        else:
            (job_dir / "DEPLOY_CHECK.md").write_text(
                f"# DEPLOY CHECK\nDecision: {decision}\n"
                "## Justification\n- tests pass\n- risks documented\n",
                encoding="utf-8")
        return {"output": "wrote DEPLOY_CHECK.md"}

    monkeypatch.setattr(
        deploy_check_wf, "_decision_prompt_node",
        lambda: Node(id="decision", kind="prompt", run=fake_decision,
                     description="fake decision (hermetic)"))
    return state


def _make_gate(hop):
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)
    return gate


def _submit(tmp_path):
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    gate = _make_gate(deploy_check_hop())
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("deploy-check", {"task": "check readiness"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "solution.py").write_text(GOOD_SOLUTION, encoding="utf-8")
    (job_dir / "test_solution.py").write_text(
        "from solution import add\ndef test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8")
    return ex, job, job_dir


def test_deploy_check_ships_with_decision(tmp_path, monkeypatch):
    """preflight -> env-scan -> validate -> risk -> decision all pass -> SHIP."""
    _install_fake_prompts(monkeypatch)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(deploy_check_hop().workflow, job,
                     {"task": "check readiness"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert (job_dir / "PREFLIGHT.md").exists()
    assert (job_dir / "ENV_SCAN.md").exists()
    assert (job_dir / "EVAL.json").exists()
    assert (job_dir / "RISK.md").exists()
    decision = (job_dir / "DEPLOY_CHECK.md").read_text()
    assert "Decision: GO" in decision


def test_deploy_check_fix_loop_when_decision_missing(tmp_path, monkeypatch):
    """First decision lacks Decision line -> FIX; retry adds it -> SHIP."""
    state = _install_fake_prompts(monkeypatch, flaky=True)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(deploy_check_hop().workflow, job,
                     {"task": "check readiness"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert state["decision_calls"] == 2
    assert "Decision: GO" in (job_dir / "DEPLOY_CHECK.md").read_text()


def test_deploy_check_no_go_blocks(tmp_path, monkeypatch):
    """Decision: NO-GO + failing EVAL -> gate BLOCKs (not SHIP)."""
    _install_fake_prompts(monkeypatch, decision="NO-GO")
    ex, job, job_dir = _submit(tmp_path)
    # break the tests -> EVAL fails
    (job_dir / "test_solution.py").write_text(
        "from solution import add\ndef test_add():\n    assert add(2, 3) == 6\n",
        encoding="utf-8")

    res = ex.execute(deploy_check_hop().workflow, job,
                     {"task": "check readiness"})
    assert res["verdict"]["verdict"] != "SHIP"
    assert res["verdict"]["eval_results"]["eval-json"]["passed"] is False


def test_deploy_check_fails_loud_without_api_key(tmp_path):
    """Without GEMINI_API_KEY the real risk prompt node raises WorkflowError."""
    hop = deploy_check_hop()
    gate = _make_gate(hop)

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("deploy-check", {"task": "check readiness"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "solution.py").write_text(GOOD, encoding="utf-8")

    with pytest.raises(WorkflowError):
        ex.execute(hop.workflow, job, {"task": "check readiness"})
