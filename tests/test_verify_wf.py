"""verify workflow tests - hermetic, model-or-fail.

The bash nodes (collect/check) run for real in a tmp job dir; the model
nodes (claims/verdict) are replaced by fakes. The star under test is the
HONESTY gate - the cop that audits the cops: a report that hides a
mechanical FAIL, drops a claim, or returns a verdict that ignores the
evidence is BLOCKed, while an HONEST UNVERIFIED audit still SHIPs (the lane
ships the audit, and the audit says the work doesn't hold up).
"""
import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["GEMINI_API_KEY"] = ""
os.environ["NINE_LLM_BACKEND"] = ""

from nine.gates.evidence import EvidenceGate
from nine.ledger.ledger import JSONLLedger
from nine.runtime.workflows import Node, WorkflowError, WorkflowExecutor
from nine.workflows.verify_wf import verify_hop

CLAIMS_ALL_PASS = (
    "# Claims\n\n"
    "1. `solution.py` exists and compiles\n"
    "2. the tests in EVAL.json pass\n"
)


def _install_fakes(monkeypatch, claims_text=CLAIMS_ALL_PASS,
                   verdict_fn=None):
    """Replace the two model nodes with hermetic fakes."""
    from nine.workflows import verify_wf

    def fake_claims(inputs, job_dir):
        (Path(job_dir) / "CLAIMS.md").write_text(
            claims_text, encoding="utf-8")
        return {"output": "wrote CLAIMS.md",
                "artifact_path": str(Path(job_dir) / "CLAIMS.md")}

    def faithful_verdict(inputs, job_dir):
        """Default fake: mirror CHECKS.json exactly (an honest cop)."""
        checks = json.loads(
            (Path(job_dir) / "CHECKS.json").read_text(encoding="utf-8"))
        claims = [{"claim": c["claim"], "status": c["status"],
                   "evidence": c["evidence"] or "checked"} for c in
                  checks["claims"]]
        statuses = [c["status"] for c in claims]
        if "FAIL" in statuses:
            verdict = "UNVERIFIED"
        elif "UNCHECKED" in statuses:
            verdict = "PARTIAL"
        else:
            verdict = "VERIFIED"
        data = {"verdict": verdict,
                "summary": f"faithful cop reports {verdict}",
                "claims": claims}
        (Path(job_dir) / "VERIFIED.json").write_text(
            json.dumps(data, indent=2), encoding="utf-8")
        return {"output": "wrote VERIFIED.json",
                "artifact_path": str(Path(job_dir) / "VERIFIED.json")}

    monkeypatch.setattr(
        verify_wf, "_claims_prompt_node",
        lambda: Node(id="claims", kind="prompt", run=fake_claims))
    monkeypatch.setattr(
        verify_wf, "_verdict_prompt_node",
        lambda: Node(id="verdict", kind="prompt",
                     run=verdict_fn or faithful_verdict))


def _make_gate(hop):
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)
    return gate


def _submit(tmp_path, seed_solution=True, seed_eval=True):
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    gate = _make_gate(verify_hop())
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("verify", {"task": "verify the work"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    if seed_solution:
        (job_dir / "solution.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8")
    if seed_eval:
        (job_dir / "EVAL.json").write_text(
            json.dumps({"passed": 2, "failed": 0}), encoding="utf-8")
    return ex, job, job_dir


def test_verify_ships_when_all_claims_pass(tmp_path, monkeypatch):
    """collect -> claims -> check -> verdict -> honest VERIFIED -> SHIP."""
    _install_fakes(monkeypatch)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(verify_hop().workflow, job,
                     {"task": "verify the work"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert res["verdict"]["eval_results"]["honesty"]["passed"] is True
    verified = json.loads(
        (job_dir / "VERIFIED.json").read_text(encoding="utf-8"))
    assert verified["verdict"] == "VERIFIED"
    checks = json.loads(
        (job_dir / "CHECKS.json").read_text(encoding="utf-8"))
    assert checks["claim_count"] == 2
    assert all(c["status"] == "PASS" for c in checks["claims"])
    assert "solution.py" in (job_dir / "VERIFY_INVENTORY.md").read_text(
        encoding="utf-8")


def test_verify_blocks_when_report_hides_mechanical_fail(
        tmp_path, monkeypatch):
    """A mechanical FAIL reported PASS is a lie - the honesty gate BLOCKs."""
    claims_with_fail = (
        "# Claims\n\n"
        "1. `missing_module.py` is present\n"
        "2. the tests in EVAL.json pass\n"
    )

    def lying_verdict(inputs, job_dir):
        data = {
            "verdict": "VERIFIED",
            "summary": "all good",
            "claims": [
                {"claim": "`missing_module.py` is present",
                 "status": "PASS", "evidence": "I looked, it is fine"},
                {"claim": "the tests in EVAL.json pass",
                 "status": "PASS", "evidence": "tests pass"},
            ],
        }
        (Path(job_dir) / "VERIFIED.json").write_text(
            json.dumps(data, indent=2), encoding="utf-8")
        return {"output": "wrote VERIFIED.json"}

    _install_fakes(monkeypatch, claims_text=claims_with_fail,
                   verdict_fn=lying_verdict)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(verify_hop().workflow, job,
                     {"task": "verify the work"})
    assert res["verdict"]["verdict"] != "SHIP"
    assert job.status == "blocked"
    honesty = res["verdict"]["eval_results"]["honesty"]
    assert honesty["passed"] is False
    assert "a cop cannot hide a failed check" in honesty["message"]


def test_verify_ships_honest_unverified(tmp_path, monkeypatch):
    """The core philosophy: an HONEST audit that the work fails SHIPs the
    audit - the inner verdict says UNVERIFIED, and that is the truth."""
    claims_with_fail = (
        "# Claims\n\n"
        "1. `missing_module.py` is present\n"
        "2. the tests in EVAL.json pass\n"
    )
    _install_fakes(monkeypatch, claims_text=claims_with_fail)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(verify_hop().workflow, job,
                     {"task": "verify the work"})
    assert res["verdict"]["verdict"] == "SHIP"
    verified = json.loads(
        (job_dir / "VERIFIED.json").read_text(encoding="utf-8"))
    assert verified["verdict"] == "UNVERIFIED"
    assert verified["claims"][0]["status"] == "FAIL"


def test_verify_blocks_when_claim_dropped(tmp_path, monkeypatch):
    """A report that drops a claim is a lie - claim count must match."""
    def drop_claim_verdict(inputs, job_dir):
        data = {
            "verdict": "VERIFIED",
            "summary": "first claim only",
            "claims": [
                {"claim": "`solution.py` exists and compiles",
                 "status": "PASS", "evidence": "exists"},
            ],
        }
        (Path(job_dir) / "VERIFIED.json").write_text(
            json.dumps(data, indent=2), encoding="utf-8")
        return {"output": "wrote VERIFIED.json"}

    _install_fakes(monkeypatch, verdict_fn=drop_claim_verdict)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(verify_hop().workflow, job,
                     {"task": "verify the work"})
    assert res["verdict"]["verdict"] != "SHIP"
    assert job.status == "blocked"
    honesty = res["verdict"]["eval_results"]["honesty"]
    assert honesty["passed"] is False
    assert "claim count mismatch" in honesty["message"]


def test_verify_blocks_when_verdict_ignores_statuses(tmp_path, monkeypatch):
    """All PASS but verdict PARTIAL - the verdict must follow the evidence."""
    def wrong_verdict(inputs, job_dir):
        data = {
            "verdict": "PARTIAL",
            "summary": "kinda ok",
            "claims": [
                {"claim": "`solution.py` exists and compiles",
                 "status": "PASS", "evidence": "exists"},
                {"claim": "the tests in EVAL.json pass",
                 "status": "PASS", "evidence": "tests pass"},
            ],
        }
        (Path(job_dir) / "VERIFIED.json").write_text(
            json.dumps(data, indent=2), encoding="utf-8")
        return {"output": "wrote VERIFIED.json"}

    _install_fakes(monkeypatch, verdict_fn=wrong_verdict)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(verify_hop().workflow, job,
                     {"task": "verify the work"})
    assert res["verdict"]["verdict"] != "SHIP"
    assert job.status == "blocked"
    honesty = res["verdict"]["eval_results"]["honesty"]
    assert honesty["passed"] is False
    assert "does not match the evidence" in honesty["message"]


def test_verify_ships_partial_when_claim_uncheckable(tmp_path, monkeypatch):
    """An UNCHECKED claim honestly reports PARTIAL - and that SHIPs."""
    claims_with_unchecked = (
        "# Claims\n\n"
        "1. `solution.py` exists and compiles\n"
        "2. the tests in EVAL.json pass\n"
        "3. the widget is delightful to use\n"
    )
    _install_fakes(monkeypatch, claims_text=claims_with_unchecked)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(verify_hop().workflow, job,
                     {"task": "verify the work"})
    assert res["verdict"]["verdict"] == "SHIP"
    verified = json.loads(
        (job_dir / "VERIFIED.json").read_text(encoding="utf-8"))
    assert verified["verdict"] == "PARTIAL"
    assert verified["claims"][2]["status"] == "UNCHECKED"


def test_verify_fails_loud_without_api_key(tmp_path):
    """Model-or-fail: no LLM key -> WorkflowError, never a canned audit."""
    hop = verify_hop()
    gate = _make_gate(hop)
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("verify", {"task": "verify the work"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "solution.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(WorkflowError):
        ex.execute(hop.workflow, job, {"task": "verify the work"})


def test_verify_routes_from_keywords():
    """'verify ...' routes to the verify lane via the keyword substrate."""
    from nine.registry import HOP_DESCRIPTIONS, KEYWORDS
    from nine.router.classifier import Router

    r = Router(model=None, version="test")
    for wf_id, kws in KEYWORDS.items():
        r.register(wf_id, kws, HOP_DESCRIPTIONS.get(wf_id, ""))
    d = r.classify("verify this work actually does what the task says")
    assert d.workflow_id == "verify"
