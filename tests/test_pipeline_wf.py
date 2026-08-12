"""pipeline workflow tests - hermetic, model-or-fail."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

os.environ["GEMINI_API_KEY"] = ""

from nine.gates.evidence import EvidenceGate
from nine.ledger.ledger import JSONLLedger
from nine.runtime.workflows import Node, WorkflowError, WorkflowExecutor
from nine.workflows.pipeline_wf import pipeline_hop

CSV = "name,age\nalice,30\nbob,25\ncarol,35\n"
STAGE = json.dumps([{"name": "alice", "age": 30},
                    {"name": "bob", "age": 25},
                    {"name": "carol", "age": 35}])


def _fake_transform(flaky=False):
    state = {"calls": 0}

    def fake(inputs, job_dir):
        state["calls"] += 1
        if flaky and state["calls"] == 1:
            (Path(job_dir) / "STAGE.json").write_text("{bad", encoding="utf-8")
            return {"output": "bad first attempt"}
        (Path(job_dir) / "STAGE.json").write_text(STAGE, encoding="utf-8")
        return {"output": "wrote STAGE.json"}

    return fake


def _make_gate(hop):
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)
    return gate


def _submit(tmp_path):

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    gate = _make_gate(pipeline_hop())
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("pipeline", {"task": "etl pipeline on this csv"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "input.csv").write_text(CSV, encoding="utf-8")
    return ex, job, job_dir


def test_pipeline_ships_etl(tmp_path, monkeypatch):
    from nine.workflows import pipeline_wf

    monkeypatch.setattr(
        pipeline_wf, "_transform_tool_node",
        lambda: Node(id="transform", kind="tool", run=_fake_transform()))
    ex, job, job_dir = _submit(tmp_path)
    res = ex.execute(pipeline_hop().workflow, job, {"task": "etl pipeline on this csv"})
    assert res["verdict"]["verdict"] == "SHIP"
    out = json.loads((job_dir / "OUTPUT.json").read_text(encoding="utf-8"))
    assert out["loaded"] == 3
    assert (job_dir / "LOAD.md").exists()
    assert (job_dir / "EVAL.json").exists()


def test_pipeline_fix_loop_on_bad_stage(tmp_path, monkeypatch):
    from nine.workflows import pipeline_wf

    monkeypatch.setattr(
        pipeline_wf, "_transform_tool_node",
        lambda: Node(id="transform", kind="tool",
                     run=_fake_transform(flaky=True)))
    ex, job, job_dir = _submit(tmp_path)
    res = ex.execute(pipeline_hop().workflow, job, {"task": "etl pipeline on this csv"})
    assert res["verdict"]["verdict"] == "SHIP"


def test_pipeline_blocks_when_stage_empty(tmp_path, monkeypatch):
    from nine.workflows import pipeline_wf

    def fake(inputs, job_dir):
        (Path(job_dir) / "STAGE.json").write_text("[]", encoding="utf-8")
        return {"output": "empty stage"}

    monkeypatch.setattr(pipeline_wf, "_transform_tool_node",
                        lambda: Node(id="transform", kind="tool", run=fake))
    ex, job, job_dir = _submit(tmp_path)
    res = ex.execute(pipeline_hop().workflow, job, {"task": "etl pipeline on this csv"})
    assert res["verdict"]["verdict"] != "SHIP"
    assert res["verdict"]["eval_results"]["output-json"]["passed"] is False


def test_pipeline_fails_loud_without_api_key(tmp_path):
    hop = pipeline_hop()
    gate = _make_gate(hop)
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("pipeline", {"task": "etl pipeline on this csv"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "input.csv").write_text(CSV, encoding="utf-8")
    with pytest.raises(WorkflowError):
        ex.execute(hop.workflow, job, {"task": "etl pipeline on this csv"})
