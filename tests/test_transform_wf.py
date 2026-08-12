"""transform workflow tests - hermetic, model-or-fail.

detect-format + validate run for real; the transform model node is faked.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

os.environ["GEMINI_API_KEY"] = ""

from nine.gates.evidence import EvidenceGate
from nine.ledger.ledger import JSONLLedger
from nine.runtime.workflows import Node, WorkflowError, WorkflowExecutor
from nine.workflows.transform_wf import transform_hop

CSV = "name,age,city\nalice,30,nyc\nbob,25,sf\ncarol,35,chicago\n"


def _fake_transform(output="OUTPUT.json", content=None, flaky=False):
    state = {"calls": 0}

    def fake(inputs, job_dir):
        state["calls"] += 1
        if flaky and state["calls"] == 1:
            (Path(job_dir) / "TARGET.txt").write_text("json", encoding="utf-8")
            (Path(job_dir) / output).write_text("{invalid json", encoding="utf-8")
            return {"output": "bad first attempt"}
        (Path(job_dir) / "TARGET.txt").write_text(
            output.split(".")[-1], encoding="utf-8")
        body = content if content is not None else (
            '[{"name": "alice", "age": 30}, {"name": "bob", "age": 25}]'
        )
        (Path(job_dir) / output).write_text(body, encoding="utf-8")
        return {"output": f"wrote {output}"}

    return fake


def _make_gate(hop):
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)
    return gate


def _submit(tmp_path, task="convert this csv to json"):

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    gate = _make_gate(transform_hop())
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("transform", {"task": task})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "data.csv").write_text(CSV, encoding="utf-8")
    return ex, job, job_dir


def test_transform_ships_csv_to_json(tmp_path, monkeypatch):
    from nine.workflows import transform_wf

    monkeypatch.setattr(
        transform_wf, "_transform_tool_node",
        lambda: Node(id="transform", kind="tool", run=_fake_transform()))
    ex, job, job_dir = _submit(tmp_path)
    res = ex.execute(transform_hop().workflow, job, {"task": "convert this csv to json"})
    assert res["verdict"]["verdict"] == "SHIP"
    fmt = (job_dir / "FORMAT.md").read_text(encoding="utf-8")
    assert "csv" in fmt
    out = (job_dir / "OUTPUT.json").read_text(encoding="utf-8")
    assert "alice" in out
    assert (job_dir / "EVAL.json").exists()


def test_transform_fix_loop_on_bad_output(tmp_path, monkeypatch):
    from nine.workflows import transform_wf

    monkeypatch.setattr(
        transform_wf, "_transform_tool_node",
        lambda: Node(id="transform", kind="tool",
                     run=_fake_transform(flaky=True)))
    ex, job, job_dir = _submit(tmp_path)
    res = ex.execute(transform_hop().workflow, job, {"task": "convert this csv to json"})
    assert res["verdict"]["verdict"] == "SHIP"


def test_transform_blocks_when_output_missing(tmp_path, monkeypatch):
    from nine.workflows import transform_wf

    def fake(inputs, job_dir):
        (Path(job_dir) / "TARGET.txt").write_text("json", encoding="utf-8")
        return {"output": "no output written"}

    monkeypatch.setattr(transform_wf, "_transform_tool_node",
                        lambda: Node(id="transform", kind="tool", run=fake))
    ex, job, job_dir = _submit(tmp_path)
    res = ex.execute(transform_hop().workflow, job, {"task": "convert this csv to json"})
    assert res["verdict"]["verdict"] != "SHIP"
    assert res["verdict"]["eval_results"]["output"]["passed"] is False


def test_transform_fails_loud_without_api_key(tmp_path):
    hop = transform_hop()
    gate = _make_gate(hop)
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("transform", {"task": "convert this csv to json"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "data.csv").write_text(CSV, encoding="utf-8")
    with pytest.raises(WorkflowError):
        ex.execute(hop.workflow, job, {"task": "convert this csv to json"})
