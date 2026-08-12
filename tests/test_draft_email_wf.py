"""draft-email workflow tests - hermetic, model-or-fail."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

os.environ["GEMINI_API_KEY"] = ""

from nine.gates.evidence import EvidenceGate
from nine.ledger.ledger import JSONLLedger
from nine.runtime.workflows import Node, WorkflowError, WorkflowExecutor
from nine.workflows.draft_email_wf import draft_email_hop

EMAIL_V1 = "Subject: Meeting\n\nHi Alex, can we sync Thursday?\n"
TONE_REVIEW = (
    "Tone: REVISE\n\nTone check: too terse for a 'warm' spec.\n"
    "Adjustments:\n1. Add a greeting line.\n2. Soften the ask.\n"
)
EMAIL_FINAL = (
    "Subject: Meeting\n\nHi Alex,\n\nHope you're well! Could we "
    "find time Thursday to sync?\n\nBest,\nSam\n"
)
REVISION = "- [x] Adjustment 1: add greeting -> added 'Hi Alex'.\n"


def _install_fakes(monkeypatch, flaky=False, never=False):
    """Replace the three prompt nodes with hermetic fakes."""
    from nine.workflows import draft_email_wf

    state = {"calls": 0}

    def fake_draft(inputs, job_dir):
        (Path(job_dir) / "DRAFT.md").write_text(EMAIL_V1, encoding="utf-8")
        return {"output": "wrote DRAFT.md"}

    def fake_reviewtone(inputs, job_dir):
        (Path(job_dir) / "TONE_REVIEW.md").write_text(
            TONE_REVIEW, encoding="utf-8")
        return {"output": "wrote TONE_REVIEW.md"}

    def fake_revise(inputs, job_dir):
        state["calls"] += 1
        (Path(job_dir) / "DRAFT.md").write_text(EMAIL_FINAL, encoding="utf-8")
        if flaky and state["calls"] == 1:
            (Path(job_dir) / "TONE_REVISION.md").write_text(
                "", encoding="utf-8")
        elif never:
            (Path(job_dir) / "TONE_REVISION.md").write_text(
                "", encoding="utf-8")
        else:
            (Path(job_dir) / "TONE_REVISION.md").write_text(
                REVISION, encoding="utf-8")
        return {"output": "wrote final DRAFT.md + log"}

    monkeypatch.setattr(draft_email_wf, "_draft_prompt_node",
                        lambda: Node(id="draft", kind="prompt", run=fake_draft))
    monkeypatch.setattr(draft_email_wf, "_reviewtone_prompt_node",
                        lambda: Node(id="reviewtone", kind="prompt",
                                     run=fake_reviewtone))
    monkeypatch.setattr(draft_email_wf, "_revise_prompt_node",
                        lambda: Node(id="revise", kind="prompt",
                                     run=fake_revise))
    return state


def _make_gate(hop):
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)
    return gate


def _submit(tmp_path):
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    gate = _make_gate(draft_email_hop())
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("draft-email",
                        {"task": "warm follow-up email to Alex"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    return ex, job, job_dir


def test_draft_email_ships_with_final(tmp_path, monkeypatch):
    """draft -> reviewtone -> revise -> SHIP with final DRAFT.md."""
    _install_fakes(monkeypatch)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(draft_email_hop().workflow, job,
                     {"task": "warm follow-up email to Alex"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert (job_dir / "DRAFT.md").read_text(encoding="utf-8") == EMAIL_FINAL
    assert "Tone: REVISE" in (job_dir / "TONE_REVIEW.md").read_text(
        encoding="utf-8")
    assert "Adjustment 1" in (job_dir / "TONE_REVISION.md").read_text(
        encoding="utf-8")


def test_draft_email_fix_loop_when_empty_log(tmp_path, monkeypatch):
    """First TONE_REVISION.md empty -> FIX; retry -> SHIP."""
    state = _install_fakes(monkeypatch, flaky=True)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(draft_email_hop().workflow, job,
                     {"task": "warm follow-up email to Alex"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert state["calls"] == 2


def test_draft_email_blocks_when_no_tone_verdict(tmp_path, monkeypatch):
    """TONE_REVIEW.md missing 'Tone:' -> tone-verdict check fails."""
    from nine.workflows import draft_email_wf

    def fake_reviewtone(inputs, job_dir):
        (Path(job_dir) / "TONE_REVIEW.md").write_text(
            "# Review\nLooks fine.\n", encoding="utf-8")
        return {"output": "wrote TONE_REVIEW.md"}

    _install_fakes(monkeypatch)
    # override reviewtone AFTER _install_fakes so the no-verdict fake wins
    monkeypatch.setattr(draft_email_wf, "_reviewtone_prompt_node",
                        lambda: Node(id="reviewtone", kind="prompt",
                                     run=fake_reviewtone))
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(draft_email_hop().workflow, job,
                     {"task": "warm follow-up email to Alex"})
    assert res["verdict"]["verdict"] != "SHIP"
    assert res["verdict"]["eval_results"]["tone-verdict"]["passed"] is False


def test_draft_email_fails_loud_without_api_key(tmp_path):
    """Without GEMINI_API_KEY the real drafter raises WorkflowError."""
    hop = draft_email_hop()
    gate = _make_gate(hop)

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("draft-email",
                        {"task": "warm follow-up email to Alex"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)

    with pytest.raises(WorkflowError):
        ex.execute(hop.workflow, job,
                   {"task": "warm follow-up email to Alex"})
