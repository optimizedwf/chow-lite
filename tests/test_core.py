"""chow-lite core test suite — router, ledger, gates, workflow executor.

Run:  python -m pytest tests/ -v
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chowlite.gates.evidence import (
    EvidenceGate,
    eval_json_check,
    exit_codes_check,
    required_artifact_check,
)
from chowlite.ledger.ledger import InvalidTransition, JSONLLedger
from chowlite.router.classifier import Router, redact
from chowlite.runtime.workflows import Node, Workflow, WorkflowError, WorkflowExecutor

# ---------- router ----------

def test_keyword_routing():
    r = Router()
    r.register("research", ["research", "investigate"], "findings doc")
    r.register("build", ["build", "implement"], "build artifacts")
    d = r.classify("please research the market")
    assert d.workflow_id == "research"
    d2 = r.classify("build me a thing")
    assert d2.workflow_id == "build"


def test_unknown_task_falls_back():
    r = Router()
    r.register("build", ["build"], "build")
    d = r.classify("zzz qqq unknown thing")
    assert d.workflow_id == "fallback-respond"


def test_redaction():
    out = redact("password=SECRET123 and Bearer abc.def.ghi")
    assert "SECRET123" not in out and "abc.def.ghi" not in out
    assert "***" in out
    out2 = redact("-----BEGIN RSA PRIVATE KEY-----\nxxxxx\n-----END RSA PRIVATE KEY-----")
    assert "xxxxx" not in out2
    assert "PRIVATE KEY" in out2  # label kept for legibility, material masked


def test_route_decision_schema_fields():
    r = Router()
    r.register("build", ["build"], "build")
    d = r.classify("build the api")
    dd = d.to_dict()
    for field in ("decision_id", "task_redacted", "workflow_id", "confidence",
                  "reason", "decided_at", "router_version", "model"):
        assert field in dd, f"missing {field}"


# ---------- ledger ----------

@pytest.fixture()
def ledger(tmp_path):
    return JSONLLedger(tmp_path / "ledger.jsonl")


def test_job_lifecycle(ledger):
    job = ledger.submit("build", {"task": "x"})
    assert job.status == "submitted"
    ledger.transition(job.job_id, "routing")
    ledger.transition(job.job_id, "running")
    ledger.transition(job.job_id, "awaiting_evidence")
    ledger.transition(job.job_id, "shipped")
    assert ledger.get(job.job_id).status == "shipped"


def test_illegal_transition(ledger):
    job = ledger.submit("build")
    with pytest.raises(InvalidTransition):
        ledger.transition(job.job_id, "shipped")  # submitted -> shipped is illegal


def test_ledger_persistence(tmp_path):
    lp = tmp_path / "ledger.jsonl"
    l1 = JSONLLedger(lp)
    j = l1.submit("build")
    l1.transition(j.job_id, "routing")
    l2 = JSONLLedger(lp)  # reload
    assert l2.get(j.job_id).status == "routing"


def test_cancel_and_recover(ledger):
    j = ledger.submit("build")
    ledger.cancel(j.job_id)
    assert ledger.get(j.job_id).status == "cancelled"

    j2 = ledger.submit("research")
    ledger.transition(j2.job_id, "routing")
    ledger.transition(j2.job_id, "running")
    ledger.transition(j2.job_id, "failed")
    ledger.recover(j2.job_id)
    assert ledger.get(j2.job_id).status == "recovered"


def test_stats(ledger):
    ledger.submit("a")
    ledger.submit("b")
    s = ledger.stats()
    assert s["total"] == 2
    assert s["by_status"]["submitted"] == 2


# ---------- gates ----------

def test_gate_ship_when_checks_pass(tmp_path):
    d = tmp_path / "job"
    d.mkdir()
    (d / "FINAL_REPORT.md").write_text("ok")
    (d / "EVAL.json").write_text(json.dumps({
        "checks": [{"name": "a", "passed": True}, {"name": "b", "passed": True}]
    }))
    gate = EvidenceGate()
    gate.register_check("eval-json", eval_json_check())
    gate.register_check("artifacts", required_artifact_check(["FINAL_REPORT.md"]))
    v = gate.evaluate({"artifact_paths": []}, d)
    assert v["verdict"] == "SHIP"


def test_gate_fix_when_check_fails(tmp_path):
    d = tmp_path / "job"
    d.mkdir()
    (d / "FINAL_REPORT.md").write_text("ok")
    (d / "EVAL.json").write_text(json.dumps({
        "checks": [{"name": "a", "passed": False}]
    }))
    gate = EvidenceGate()
    gate.register_check("eval-json", eval_json_check())
    v = gate.evaluate({"artifact_paths": []}, d)
    assert v["verdict"] == "FIX"


def test_gate_block_when_no_evidence(tmp_path):
    gate = EvidenceGate()  # no checks registered
    v = gate.evaluate({"artifact_paths": []}, tmp_path)
    assert v["verdict"] == "BLOCK"


def test_eval_json_missing(tmp_path):
    gate = EvidenceGate()
    gate.register_check("eval-json", eval_json_check())
    v = gate.evaluate({"artifact_paths": []}, tmp_path)
    assert v["verdict"] == "FIX" or v["verdict"] == "BLOCK"


# ---------- workflow executor ----------

def make_wf():
    wf = Workflow(id="demo")
    wf.add_node(Node(id="gen", kind="bash",
                     command="echo hello > out.txt; "
                             "printf '{\"checks\":[{\"name\":\"c\",\"passed\":true}]}' > EVAL.json; "
                             "echo done > FINAL_REPORT.md"))
    return wf


def test_executor_ships(tmp_path):
    ledger = JSONLLedger(tmp_path / "l.jsonl")
    gate = EvidenceGate()
    gate.register_check("eval-json", eval_json_check())
    gate.register_check("artifacts", required_artifact_check(["FINAL_REPORT.md"]))
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("demo")
    result = ex.execute(make_wf(), job, {"task": "demo"})
    assert result["verdict"]["verdict"] == "SHIP"
    assert ledger.get(job.job_id).status == "shipped"
    # artifacts auto-detected
    names = {a["name"] for a in result["artifacts"]}
    assert {"out.txt", "EVAL.json", "FINAL_REPORT.md"} <= names
    # artifact manifest has sha256 + size
    a = result["artifacts"][0]
    assert a["sha256"] and a["size"] > 0


def test_executor_cycle_detection():
    wf = Workflow(id="cyc")
    wf.add_node(Node(id="a", kind="bash", command="echo 1", depends_on=["b"]))
    wf.add_node(Node(id="b", kind="bash", command="echo 2", depends_on=["a"]))
    with pytest.raises(WorkflowError):
        wf.topological_order()


def test_nonzero_exit_is_fix_evidence_not_crash(tmp_path):
    """Doctrine: a shell exit code is NOT task success — non-zero exit is
    failing evidence, so the gate returns FIX and the job goes to fixing."""
    ledger = JSONLLedger(tmp_path / "l.jsonl")
    gate = EvidenceGate()
    gate.register_check("exit-codes", exit_codes_check())
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("boom")
    wf = Workflow(id="boom")
    wf.add_node(Node(id="bad", kind="bash", command="exit 3"))
    result = ex.execute(wf, job, {"task": "x"})
    assert result["verdict"]["verdict"] == "FIX"
    assert ledger.get(job.job_id).status == "fixing"


def test_exception_marks_job_failed(tmp_path):
    def boom_run(inputs, job_dir):
        raise RuntimeError("tool exploded")

    ledger = JSONLLedger(tmp_path / "l.jsonl")
    gate = EvidenceGate()
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("boom")
    wf = Workflow(id="boom")
    wf.add_node(Node(id="bad", kind="tool", run=boom_run))
    with pytest.raises(WorkflowError):
        ex.execute(wf, job, {"task": "x"})
    assert ledger.get(job.job_id).status == "failed"


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))


def test_model_crash_falls_back_to_keywords(monkeypatch):
    """A model exception (quota 429, timeout) must never crash routing."""
    import chowlite.router.classifier as _c

    monkeypatch.setattr(_c, "_RETRY_DELAYS", (0.01, 0.01))
    from chowlite.router.classifier import Router

    class ExplodingModel:
        def generate_content(self, prompt):
            raise RuntimeError("429 RESOURCE_EXHAUSTED")

    r = Router()
    r.register("research", ["research", "investigate"])
    r.register("build", ["build", "implement"])
    from chowlite.router.classifier import GeminiRouter

    r.model_router = GeminiRouter(ExplodingModel(), r.workflows)
    d = r.classify("research the printing press")
    assert d.workflow_id == "research"
    assert d.model == "deterministic-keyword"
    assert "model unavailable" in d.reason


def test_model_unknown_workflow_falls_back_to_keywords():
    from chowlite.router.classifier import GeminiRouter, Router

    class LyingModel:
        def generate_content(self, prompt):
            class R:
                text = '{"workflow_id": "made-up-wf", "confidence": 0.99, "reason": "x"}'

            return R()

    r = Router()
    r.register("research", ["research"])
    r.model_router = GeminiRouter(LyingModel(), r.workflows)
    d = r.classify("research something")
    assert d.workflow_id == "research"  # invented id rejected, keyword used
    assert "unknown workflow" in d.reason
