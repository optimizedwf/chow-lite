"""research-quick workflow tests - hermetic, model-or-fail.

Tests inject fake prompt + ADK nodes via monkeypatch; without GEMINI_API_KEY
the real prompt/ADK nodes fail loud (WorkflowError).
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
from nine.workflows.research_quick_wf import research_quick_hop

GOOD = (
    "import os\n"
    "def add(a, b):\n    return a + b\n"
    "def main():\n    print(add(2, 3))\n"
    "if __name__ == '__main__':\n    main()\n"
)


def _install_fake_model_nodes(monkeypatch, sections=True, flaky=False):
    """Replace search-prep + researcher nodes with hermetic fakes."""
    from nine.workflows import research_quick_wf

    state = {"prep_calls": 0, "research_calls": 0}

    def fake_prep(inputs, job_dir):
        job_dir = Path(job_dir)
        state["prep_calls"] += 1
        (job_dir / "SEARCH_PLAN.md").write_text(
            "# SEARCH PLAN\n## Research Question\nWhat does this code do?\n"
            "## Focus Areas\n- entrypoint\n- public functions\n"
            "## FINDINGS.md Outline\nSummary, Details, Evidence, Recommendations\n",
            encoding="utf-8")
        return {"output": "wrote SEARCH_PLAN.md"}

    monkeypatch.setattr(
        research_quick_wf, "_search_prep_prompt_node",
        lambda: Node(id="search-prep", kind="prompt", run=fake_prep,
                     description="fake search-prep (hermetic)"))

    def fake_researcher(inputs, job_dir):
        job_dir = Path(job_dir)
        state["research_calls"] += 1
        if flaky and state["research_calls"] == 1:
            # first attempt: no sections -> gate FIX
            (job_dir / "FINDINGS.md").write_text(
                "no research yet\n", encoding="utf-8")
        elif sections:
            (job_dir / "FINDINGS.md").write_text(
                "# FINDINGS\n"
                "## Summary\nThe code is a two-function calculator module.\n"
                "## Details\n- `add(a, b)` sums two numbers.\n"
                "## Evidence\n- solution.py: `def add(a, b)`\n"
                "## Recommendations\n- add input validation.\n",
                encoding="utf-8")
        else:
            (job_dir / "FINDINGS.md").write_text(
                "# FINDINGS\nOnly a title, no sections.\n", encoding="utf-8")
        return {"output": "wrote FINDINGS.md"}

    monkeypatch.setattr(
        research_quick_wf, "_researcher_adk_node",
        lambda: Node(id="researcher", kind="tool", run=fake_researcher,
                     description="fake researcher (hermetic)"))
    return state


def _make_gate(hop):
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)
    return gate


def _submit(tmp_path):
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    gate = _make_gate(research_quick_hop())
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("research-quick", {"task": "what does this code do"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "solution.py").write_text(GOOD, encoding="utf-8")
    return ex, job, job_dir


def test_research_quick_ships_with_findings(tmp_path, monkeypatch):
    """search-prep -> researcher -> receipt all pass -> SHIP."""
    _install_fake_model_nodes(monkeypatch)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(research_quick_hop().workflow, job,
                     {"task": "what does this code do"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert (job_dir / "SEARCH_PLAN.md").exists()
    findings = (job_dir / "FINDINGS.md").read_text(encoding="utf-8")
    assert "## Summary" in findings and "## Evidence" in findings
    assert (job_dir / "RESEARCH_RECEIPT.json").exists()


def test_research_quick_fix_loop_when_findings_missing_sections(
        tmp_path, monkeypatch):
    """First FINDINGS.md has no sections -> FIX; retry writes them -> SHIP."""
    state = _install_fake_model_nodes(monkeypatch, flaky=True)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(research_quick_hop().workflow, job,
                     {"task": "what does this code do"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert state["research_calls"] == 2
    assert "## Summary" in (job_dir / "FINDINGS.md").read_text(encoding="utf-8")


def test_research_quick_blocks_without_sections(tmp_path, monkeypatch):
    """Researcher never writes sections -> gate stays FIX (exhausts loops)."""
    _install_fake_model_nodes(monkeypatch, sections=False)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(research_quick_hop().workflow, job,
                     {"task": "what does this code do"})
    assert res["verdict"]["verdict"] != "SHIP"
    assert res["verdict"]["eval_results"]["sections"]["passed"] is False


def test_research_quick_fails_loud_without_api_key(tmp_path):
    """Without GEMINI_API_KEY the real search-prep prompt node raises."""
    hop = research_quick_hop()
    gate = _make_gate(hop)

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("research-quick", {"task": "what does this code do"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "solution.py").write_text(GOOD, encoding="utf-8")

    with pytest.raises(WorkflowError):
        ex.execute(hop.workflow, job, {"task": "what does this code do"})
