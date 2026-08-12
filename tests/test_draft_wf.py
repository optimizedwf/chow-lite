"""draft workflow tests - hermetic, model-or-fail."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

os.environ["GEMINI_API_KEY"] = ""

from nine.gates.evidence import EvidenceGate
from nine.ledger.ledger import JSONLLedger
from nine.runtime.workflows import Node, WorkflowError, WorkflowExecutor
from nine.workflows.draft_wf import draft_hop

DRAFT_V1 = (
    "# Proposal: logging\n\n## Goal\nAdd structured logging.\n\n"
    "## Plan\n1. Add logger module\n2. Wire middleware\n"
)
REVIEW = (
    "# Review\n\n1. Finding 1 (MUST): plan lacks rollout steps.\n"
    "2. Finding 2 (SHOULD): add error-handling section.\n"
    "Verdict: REVISE\n"
)
DRAFT_FINAL = (
    "# Proposal: logging\n\n## Goal\nAdd structured logging.\n\n"
    "## Plan\n1. Add logger module\n2. Wire middleware\n"
    "3. Rollout: feature flag\n4. Error handling\n"
)
LOG = "- [x] Finding 1: plan lacks rollout steps -> added rollout step 3.\n"


def _install_fakes(monkeypatch, log_ok=True, never=False, flaky=False):
    """Replace the three model nodes with hermetic fakes."""
    from nine.workflows import draft_wf

    state = {"calls": 0}

    def fake_drafter(inputs, job_dir):
        (Path(job_dir) / "DRAFT.md").write_text(DRAFT_V1, encoding="utf-8")
        return {"output": "wrote DRAFT.md"}

    def fake_reviewer(inputs, job_dir):
        (Path(job_dir) / "REVIEW.md").write_text(REVIEW, encoding="utf-8")
        return {"output": "wrote REVIEW.md"}

    def fake_reviser(inputs, job_dir):
        state["calls"] += 1
        (Path(job_dir) / "DRAFT.md").write_text(DRAFT_FINAL, encoding="utf-8")
        if flaky and state["calls"] == 1:
            (Path(job_dir) / "REVISION_LOG.md").write_text("", encoding="utf-8")
        elif never:
            (Path(job_dir) / "REVISION_LOG.md").write_text("", encoding="utf-8")
        elif log_ok:
            (Path(job_dir) / "REVISION_LOG.md").write_text(LOG, encoding="utf-8")
        else:
            (Path(job_dir) / "REVISION_LOG.md").write_text(
                "x", encoding="utf-8")
        return {"output": "wrote final DRAFT.md + log"}

    monkeypatch.setattr(draft_wf, "_draft_adk_node",
                        lambda: Node(id="draft", kind="tool", run=fake_drafter))
    monkeypatch.setattr(draft_wf, "_review_prompt_node",
                        lambda: Node(id="review", kind="prompt", run=fake_reviewer))
    monkeypatch.setattr(draft_wf, "_revise_adk_node",
                        lambda: Node(id="revise", kind="tool", run=fake_reviser))
    return state


def _make_gate(hop):
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)
    return gate


def _submit(tmp_path):
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    gate = _make_gate(draft_hop())
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("draft", {"task": "draft a proposal for logging"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    return ex, job, job_dir


def test_draft_ships_with_revision_log(tmp_path, monkeypatch):
    """draft -> review -> revise -> SHIP with all artifacts."""
    _install_fakes(monkeypatch)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(draft_hop().workflow, job,
                     {"task": "draft a proposal for logging"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert (job_dir / "DRAFT.md").read_text(encoding="utf-8") == DRAFT_FINAL
    assert "Finding 1" in (job_dir / "REVISION_LOG.md").read_text(
        encoding="utf-8")
    assert "Verdict" in (job_dir / "REVIEW.md").read_text(encoding="utf-8")


def test_draft_fix_loop_when_empty_log(tmp_path, monkeypatch):
    """First REVISION_LOG.md empty -> FIX; retry writes entries -> SHIP."""
    state = _install_fakes(monkeypatch, flaky=True)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(draft_hop().workflow, job,
                     {"task": "draft a proposal for logging"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert state["calls"] == 2


def test_draft_blocks_when_short_log(tmp_path, monkeypatch):
    """REVISION_LOG.md is 'x' -> revision-log check fails -> not SHIP."""
    _install_fakes(monkeypatch, log_ok=False)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(draft_hop().workflow, job,
                     {"task": "draft a proposal for logging"})
    assert res["verdict"]["verdict"] != "SHIP"
    assert res["verdict"]["eval_results"]["revision-log"]["passed"] is False


def test_draft_fails_loud_without_api_key(tmp_path):
    """Without GEMINI_API_KEY the real drafter raises WorkflowError."""
    hop = draft_hop()
    gate = _make_gate(hop)

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("draft", {"task": "draft a proposal for logging"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)

    with pytest.raises(WorkflowError):
        ex.execute(hop.workflow, job, {"task": "draft a proposal for logging"})
