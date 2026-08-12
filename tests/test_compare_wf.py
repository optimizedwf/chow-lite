"""compare workflow tests - hermetic, model-or-fail.

Inject fake nodes via monkeypatch; without GEMINI_API_KEY the real model
nodes fail loud (WorkflowError).
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
from nine.workflows.compare_wf import compare_hop

CRITERIA = (
    "# Options\n1. Redis\n2. Postgres\n3. SQLite\n"
    "# Criteria\n1. Latency (LOWER better)\n2. Durability (HIGHER better)\n"
)
OPTIONS = (
    "# Scores\n| Option | Latency | Durability |\n"
    "| Redis | 5 (in-mem) | 2 (lossy) |\n"
    "| Postgres | 3 (disk) | 5 (WAL) |\n"
    "| SQLite | 4 | 4 |\n"
)
GOOD = "# Comparison\n\nRecommendation: Postgres\n\nScorecard: ...\n"
NO_RECO = "# Comparison\n\nScorecard: ...\nNo winner here.\n"


def _install_fakes(monkeypatch, first_no_reco=False, never=False):
    """Replace the three model nodes with hermetic fakes."""
    from nine.workflows import compare_wf

    state = {"calls": 0}

    def fake_criteria(inputs, job_dir):
        (Path(job_dir) / "CRITERIA.md").write_text(CRITERIA, encoding="utf-8")
        return {"output": "wrote CRITERIA.md"}

    def fake_analyzer(inputs, job_dir):
        (Path(job_dir) / "OPTIONS.md").write_text(OPTIONS, encoding="utf-8")
        return {"output": "wrote OPTIONS.md"}

    def fake_comparator(inputs, job_dir):
        state["calls"] += 1
        if never:
            body = "# Comparison\n"
        elif first_no_reco and state["calls"] == 1:
            body = NO_RECO
        else:
            body = GOOD
        (Path(job_dir) / "COMPARISON.md").write_text(body, encoding="utf-8")
        return {"output": "wrote COMPARISON.md"}

    monkeypatch.setattr(compare_wf, "_criteria_prompt_node",
                        lambda: Node(id="criteria-extract", kind="prompt",
                                     run=fake_criteria))
    monkeypatch.setattr(compare_wf, "_analyzer_adk_node",
                        lambda: Node(id="analyzer", kind="tool",
                                     run=fake_analyzer))
    monkeypatch.setattr(compare_wf, "_comparator_prompt_node",
                        lambda: Node(id="comparator", kind="prompt",
                                     run=fake_comparator))
    return state


def _make_gate(hop):
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)
    return gate


def _submit(tmp_path):
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    gate = _make_gate(compare_hop())
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("compare", {"task": "compare Redis vs Postgres"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    return ex, job, job_dir


def test_compare_ships_with_recommendation(tmp_path, monkeypatch):
    """criteria -> analyzer -> comparator -> SHIP."""
    _install_fakes(monkeypatch)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(compare_hop().workflow, job,
                     {"task": "compare Redis vs Postgres"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert (job_dir / "CRITERIA.md").exists()
    assert (job_dir / "OPTIONS.md").exists()
    assert "Recommendation: Postgres" in (job_dir / "COMPARISON.md").read_text(
        encoding="utf-8")


def test_compare_fix_loop_when_no_recommendation(tmp_path, monkeypatch):
    """First COMPARISON.md lacks Recommendation -> FIX; retry -> SHIP."""
    state = _install_fakes(monkeypatch, first_no_reco=True)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(compare_hop().workflow, job,
                     {"task": "compare Redis vs Postgres"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert state["calls"] == 2


def test_compare_blocks_when_never_recommends(tmp_path, monkeypatch):
    """Comparator never writes a Recommendation line -> not SHIP."""
    _install_fakes(monkeypatch, never=True)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(compare_hop().workflow, job,
                     {"task": "compare Redis vs Postgres"})
    assert res["verdict"]["verdict"] != "SHIP"
    assert res["verdict"]["eval_results"]["recommendation"]["passed"] is False


def test_compare_fails_loud_without_api_key(tmp_path):
    """Without GEMINI_API_KEY the real criteria-extract node raises."""
    hop = compare_hop()
    gate = _make_gate(hop)

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("compare", {"task": "compare Redis vs Postgres"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)

    with pytest.raises(WorkflowError):
        ex.execute(hop.workflow, job, {"task": "compare Redis vs Postgres"})
