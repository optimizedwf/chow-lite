"""summarize-standalone workflow tests - hermetic, model-or-fail.

The summarizer node is promoted from the runtime summarizer; tests inject a
fake summarizer node via monkeypatch, plus one real-node test proving the
WorkflowError path without GEMINI_API_KEY.
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
from nine.workflows.summarize_standalone_wf import summarize_standalone_hop

GOOD = (
    "def add(a, b):\n    return a + b\n"
    "def main():\n    print(add(2, 3))\n"
    "if __name__ == '__main__':\n    main()\n"
)


def _install_fake_summarizer(monkeypatch, summary_ok=True, flaky=False):
    """Replace the summarizer node with a hermetic fake."""
    from nine.workflows import summarize_standalone_wf

    state = {"calls": 0}

    def fake_summarizer(inputs, job_dir):
        job_dir = Path(job_dir)
        state["calls"] += 1
        if flaky and state["calls"] == 1:
            (job_dir / "SUMMARY.md").write_text("", encoding="utf-8")
        elif summary_ok:
            (job_dir / "SUMMARY.md").write_text(
                "# Summary\n\nA calculator module with add(a, b) and a "
                "main() entrypoint.\n",
                encoding="utf-8")
        else:
            (job_dir / "SUMMARY.md").write_text("x", encoding="utf-8")
        return {"output": "wrote SUMMARY.md", "chars_in": 120,
                "chars_out": 40}

    monkeypatch.setattr(
        summarize_standalone_wf, "_summarizer_node",
        lambda: Node(id="summarize-SOURCE", kind="summarize",
                     run=fake_summarizer,
                     description="fake summarizer (hermetic)"))
    return state


def _make_gate(hop):
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)
    return gate


def _submit(tmp_path):
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    gate = _make_gate(summarize_standalone_hop())
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("summarize-standalone", {"task": "summarize this"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "solution.py").write_text(GOOD, encoding="utf-8")
    return ex, job, job_dir


def test_summarize_standalone_ships_with_summary(tmp_path, monkeypatch):
    """read-source -> summarizer -> SUMMARY.md -> SHIP."""
    _install_fake_summarizer(monkeypatch)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(summarize_standalone_hop().workflow, job,
                     {"task": "summarize this"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert (job_dir / "SOURCE.md").exists()
    summary = (job_dir / "SUMMARY.md").read_text(encoding="utf-8")
    assert "# Summary" in summary and len(summary) > 20
    assert "solution.py" in (job_dir / "SOURCE.md").read_text(
        encoding="utf-8")


def test_summarize_standalone_fix_loop_when_empty(tmp_path, monkeypatch):
    """First SUMMARY.md empty -> FIX; retry writes content -> SHIP."""
    state = _install_fake_summarizer(monkeypatch, flaky=True)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(summarize_standalone_hop().workflow, job,
                     {"task": "summarize this"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert state["calls"] == 2


def test_summarize_standalone_blocks_when_too_short(tmp_path, monkeypatch):
    """Summarizer writes only 'x' -> nonempty check fails -> not SHIP."""
    _install_fake_summarizer(monkeypatch, summary_ok=False)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(summarize_standalone_hop().workflow, job,
                     {"task": "summarize this"})
    assert res["verdict"]["verdict"] != "SHIP"
    assert res["verdict"]["eval_results"]["nonempty"]["passed"] is False


def test_summarize_standalone_fails_loud_without_api_key(tmp_path):
    """Without GEMINI_API_KEY the real summarizer raises WorkflowError."""
    hop = summarize_standalone_hop()
    gate = _make_gate(hop)

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("summarize-standalone", {"task": "summarize this"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "solution.py").write_text(GOOD, encoding="utf-8")

    with pytest.raises(WorkflowError):
        ex.execute(hop.workflow, job, {"task": "summarize this"})
