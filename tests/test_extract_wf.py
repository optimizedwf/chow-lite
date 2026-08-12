"""extract workflow tests - hermetic, model-or-fail.

Tests inject a fake extractor ADK node via monkeypatch; without
GEMINI_API_KEY the real extractor fails loud (WorkflowError).
"""
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
from nine.workflows.extract_wf import extract_hop

GOOD = (
    "import os\n"
    "API_KEY = os.environ.get('API_KEY')\n"
    "def add(a, b):\n    return a + b\n"
    "def main():\n    print(add(2, 3))\n"
    "if __name__ == '__main__':\n    main()\n"
)

GOOD_JSON = json.dumps(
    {"module": "solution", "functions": ["add", "main"],
     "constants": ["API_KEY"], "entrypoint": "main"},
    indent=2,
)


def _install_fake_extractor(monkeypatch, json_ok=True, flaky=False):
    """Replace the extractor ADK node with a hermetic fake."""
    from nine.workflows import extract_wf

    state = {"calls": 0}

    def fake_extractor(inputs, job_dir):
        job_dir = Path(job_dir)
        state["calls"] += 1
        if flaky and state["calls"] == 1:
            (job_dir / "OUTPUT.json").write_text(
                "this is not json", encoding="utf-8")
        elif json_ok:
            (job_dir / "OUTPUT.json").write_text(
                GOOD_JSON, encoding="utf-8")
        else:
            (job_dir / "OUTPUT.json").write_text("{}", encoding="utf-8")
        return {"output": "wrote OUTPUT.json"}

    monkeypatch.setattr(
        extract_wf, "_extractor_adk_node",
        lambda: Node(id="extractor", kind="tool", run=fake_extractor,
                     description="fake extractor (hermetic)"))
    return state


def _make_gate(hop):
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)
    return gate


def _submit(tmp_path):
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    gate = _make_gate(extract_hop())
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("extract", {"task": "extract functions as json"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "solution.py").write_text(GOOD, encoding="utf-8")
    return ex, job, job_dir


def test_extract_ships_with_valid_json(tmp_path, monkeypatch):
    """read-source -> extractor -> OUTPUT.json valid -> SHIP."""
    _install_fake_extractor(monkeypatch)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(extract_hop().workflow, job,
                     {"task": "extract functions as json"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert (job_dir / "SOURCE.md").exists()
    data = json.loads((job_dir / "OUTPUT.json").read_text(encoding="utf-8"))
    assert "add" in data["functions"] and "main" in data["functions"]


def test_extract_fix_loop_when_invalid_json(tmp_path, monkeypatch):
    """First OUTPUT.json is invalid -> FIX; retry valid -> SHIP."""
    state = _install_fake_extractor(monkeypatch, flaky=True)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(extract_hop().workflow, job,
                     {"task": "extract functions as json"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert state["calls"] == 2


def test_extract_blocks_on_empty_object(tmp_path, monkeypatch):
    """OUTPUT.json is {} -> valid-json check fails -> not SHIP."""
    _install_fake_extractor(monkeypatch, json_ok=False)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(extract_hop().workflow, job,
                     {"task": "extract functions as json"})
    assert res["verdict"]["verdict"] != "SHIP"
    assert res["verdict"]["eval_results"]["valid-json"]["passed"] is False


def test_extract_fails_loud_without_api_key(tmp_path):
    """Without GEMINI_API_KEY the real extractor ADK node raises."""
    hop = extract_hop()
    gate = _make_gate(hop)

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("extract", {"task": "extract functions as json"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "solution.py").write_text(GOOD, encoding="utf-8")

    with pytest.raises(WorkflowError):
        ex.execute(hop.workflow, job, {"task": "extract functions as json"})
