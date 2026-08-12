"""research-deep workflow tests - hermetic, model-or-fail.

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
from nine.workflows.research_deep_wf import research_deep_hop

GOOD = (
    "import os\n"
    "def add(a, b):\n    return a + b\n"
    "def main():\n    print(add(2, 3))\n"
    "if __name__ == '__main__':\n    main()\n"
)

DRAFT = (
    "# DRAFT FINDINGS\n## Summary\nA two-function module.\n"
    "## Details\n- add(a,b) sums\n## Evidence\n- solution.py: add\n"
    "## Open Questions\n- none\n## Recommendations\n- validate input\n"
)
CRIT = (
    "# CRITIQUE\n## Gaps\n- no error handling analysis\n"
    "## Weaknesses\n- add() allows negatives\n"
    "## Unverified Claims\n- none\n"
    "## Suggested Improvements\n- document negative inputs\n"
)
ITER = (
    "# ITERATED FINDINGS\n## Summary\nA two-function module.\n"
    "## Details\n- add(a,b) sums\n## Evidence\n- solution.py: add\n"
    "## Open Questions\n- none\n## Recommendations\n- validate input\n"
    "## Changes Made\n- added negative-input note\n"
)
FINAL = (
    "# FINDINGS\n## Summary\nA two-function module.\n"
    "## Details\n- add(a,b) sums\n## Evidence\n- solution.py: add\n"
    "## Open Questions\n- none\n## Recommendations\n- validate input\n"
    "## Critique Pass\nCritique found 1 gap; iteration resolved it.\n"
)


def _install_fake_model_nodes(monkeypatch, final_ok=True, flaky=False):
    """Replace researcher/critique/iterate/synthesize with hermetic fakes."""
    from nine.workflows import research_deep_wf

    state = {"critique_calls": 0, "synth_calls": 0}

    def fake_researcher(inputs, job_dir):
        job_dir = Path(job_dir)
        (job_dir / "DRAFT_FINDINGS.md").write_text(DRAFT, encoding="utf-8")
        return {"output": "wrote DRAFT_FINDINGS.md"}

    monkeypatch.setattr(
        research_deep_wf, "_researcher_adk_node",
        lambda: Node(id="researcher", kind="tool", run=fake_researcher,
                     description="fake researcher (hermetic)"))

    def fake_critique(inputs, job_dir):
        job_dir = Path(job_dir)
        state["critique_calls"] += 1
        (job_dir / "CRITIQUE.md").write_text(CRIT, encoding="utf-8")
        return {"output": "wrote CRITIQUE.md"}

    monkeypatch.setattr(
        research_deep_wf, "_critique_prompt_node",
        lambda: Node(id="critique", kind="prompt", run=fake_critique,
                     description="fake critique (hermetic)"))

    def fake_iterate(inputs, job_dir):
        job_dir = Path(job_dir)
        (job_dir / "ITERATED_FINDINGS.md").write_text(ITER, encoding="utf-8")
        return {"output": "wrote ITERATED_FINDINGS.md"}

    monkeypatch.setattr(
        research_deep_wf, "_iterate_adk_node",
        lambda: Node(id="iterate", kind="tool", run=fake_iterate,
                     description="fake iterate (hermetic)"))

    def fake_synthesize(inputs, job_dir):
        job_dir = Path(job_dir)
        state["synth_calls"] += 1
        if flaky and state["synth_calls"] == 1:
            (job_dir / "FINDINGS.md").write_text(
                "# FINDINGS\nno final yet\n", encoding="utf-8")
        elif final_ok:
            (job_dir / "FINDINGS.md").write_text(FINAL, encoding="utf-8")
        else:
            (job_dir / "FINDINGS.md").write_text(
                "# FINDINGS\n## Summary\nno critique pass noted\n",
                encoding="utf-8")
        return {"output": "wrote FINDINGS.md"}

    monkeypatch.setattr(
        research_deep_wf, "_synthesize_prompt_node",
        lambda: Node(id="synthesize", kind="prompt", run=fake_synthesize,
                     description="fake synthesize (hermetic)"))
    return state


def _make_gate(hop):
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)
    return gate


def _submit(tmp_path):
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    gate = _make_gate(research_deep_hop())
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("research-deep", {"task": "deep dive this module"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "solution.py").write_text(GOOD, encoding="utf-8")
    return ex, job, job_dir


def test_research_deep_ships_with_critique_pass(tmp_path, monkeypatch):
    """All five nodes run; FINDINGS.md has sections + Critique Pass -> SHIP."""
    _install_fake_model_nodes(monkeypatch)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(research_deep_hop().workflow, job,
                     {"task": "deep dive this module"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert (job_dir / "DRAFT_FINDINGS.md").exists()
    assert (job_dir / "CRITIQUE.md").exists()
    assert (job_dir / "ITERATED_FINDINGS.md").exists()
    findings = (job_dir / "FINDINGS.md").read_text(encoding="utf-8")
    assert "## Summary" in findings and "## Critique Pass" in findings
    assert (job_dir / "RESEARCH_RECEIPT.json").exists()


def test_research_deep_fix_loop_when_findings_incomplete(tmp_path, monkeypatch):
    """First synthesize lacks sections -> FIX; retry completes -> SHIP."""
    state = _install_fake_model_nodes(monkeypatch, flaky=True)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(research_deep_hop().workflow, job,
                     {"task": "deep dive this module"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert state["synth_calls"] == 2
    assert "## Critique Pass" in (job_dir / "FINDINGS.md").read_text(
        encoding="utf-8")


def test_research_deep_blocks_without_critique_pass(tmp_path, monkeypatch):
    """FINDINGS.md lacks Critique Pass -> gate stays FIX (loops exhausted)."""
    _install_fake_model_nodes(monkeypatch, final_ok=False)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(research_deep_hop().workflow, job,
                     {"task": "deep dive this module"})
    assert res["verdict"]["verdict"] != "SHIP"
    assert res["verdict"]["eval_results"]["findings"]["passed"] is False


def test_research_deep_fails_loud_without_api_key(tmp_path):
    """Without GEMINI_API_KEY the real researcher ADK node raises."""
    hop = research_deep_hop()
    gate = _make_gate(hop)

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("research-deep", {"task": "deep dive this module"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "solution.py").write_text(GOOD, encoding="utf-8")

    with pytest.raises(WorkflowError):
        ex.execute(hop.workflow, job, {"task": "deep dive this module"})
