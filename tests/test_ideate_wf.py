"""ideate workflow tests - hermetic, model-or-fail."""
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
from nine.workflows.ideate_wf import ideate_hop

EXPANDED = "# Expanded\n\nCore: a recipe app.\nAngles:\n1. AI pair chef\n2. Meal kits\n"
CHALLENGES = (
    "# Challenges\n\nWeaknesses:\n1. Crowded market\n2. Hard cold start\n"
    "Risks:\n1. HIGH - food safety\n"
)
BRIEF = "# Idea Brief\n\nPitch: AI pair chef for families.\nScope: MVP in/out.\n"
VIABILITY = json.dumps(
    {"score": 72, "strengths": ["clear niche", "family angle"],
     "risks": ["crowded market"], "verdict": "GO"},
    indent=2,
)


def _install_fakes(monkeypatch, flaky=False, never=False):
    """Replace the three prompt nodes with hermetic fakes."""
    from nine.workflows import ideate_wf

    state = {"calls": 0}

    def fake_expand(inputs, job_dir):
        (Path(job_dir) / "EXPANDED.md").write_text(EXPANDED, encoding="utf-8")
        return {"output": "wrote EXPANDED.md"}

    def fake_challenge(inputs, job_dir):
        (Path(job_dir) / "CHALLENGES.md").write_text(CHALLENGES, encoding="utf-8")
        return {"output": "wrote CHALLENGES.md"}

    def fake_refine(inputs, job_dir):
        state["calls"] += 1
        (Path(job_dir) / "IDEA_BRIEF.md").write_text(BRIEF, encoding="utf-8")
        if flaky and state["calls"] == 1:
            (Path(job_dir) / "VIABILITY.json").write_text(
                "{not json", encoding="utf-8")
        elif never:
            (Path(job_dir) / "VIABILITY.json").write_text(
                '{"score": "nope"}', encoding="utf-8")
        else:
            (Path(job_dir) / "VIABILITY.json").write_text(
                VIABILITY, encoding="utf-8")
        return {"output": "wrote brief + viability"}

    monkeypatch.setattr(ideate_wf, "_expand_prompt_node",
                        lambda: Node(id="expand", kind="prompt", run=fake_expand))
    monkeypatch.setattr(ideate_wf, "_challenge_prompt_node",
                        lambda: Node(id="challenge", kind="prompt",
                                     run=fake_challenge))
    monkeypatch.setattr(ideate_wf, "_refine_prompt_node",
                        lambda: Node(id="refine", kind="prompt", run=fake_refine))
    return state


def _make_gate(hop):
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)
    return gate


def _submit(tmp_path):
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    gate = _make_gate(ideate_hop())
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("ideate", {"task": "recipe app for families"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    return ex, job, job_dir


def test_ideate_ships_with_brief_and_viability(tmp_path, monkeypatch):
    """expand -> challenge -> refine -> SHIP with both artifacts."""
    _install_fakes(monkeypatch)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(ideate_hop().workflow, job,
                     {"task": "recipe app for families"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert (job_dir / "EXPANDED.md").exists()
    assert (job_dir / "CHALLENGES.md").exists()
    data = json.loads((job_dir / "VIABILITY.json").read_text(encoding="utf-8"))
    assert data["score"] == 72 and data["verdict"] == "GO"


def test_ideate_fix_loop_when_invalid_json(tmp_path, monkeypatch):
    """First VIABILITY.json invalid -> FIX; retry valid -> SHIP."""
    state = _install_fakes(monkeypatch, flaky=True)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(ideate_hop().workflow, job,
                     {"task": "recipe app for families"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert state["calls"] == 2


def test_ideate_blocks_when_bad_viability(tmp_path, monkeypatch):
    """VIABILITY.json missing numeric score -> viability check fails."""
    _install_fakes(monkeypatch, never=True)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(ideate_hop().workflow, job,
                     {"task": "recipe app for families"})
    assert res["verdict"]["verdict"] != "SHIP"
    assert res["verdict"]["eval_results"]["viability-json"]["passed"] is False


def test_ideate_fails_loud_without_api_key(tmp_path):
    """Without GEMINI_API_KEY the real expander raises WorkflowError."""
    hop = ideate_hop()
    gate = _make_gate(hop)

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("ideate", {"task": "recipe app for families"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)

    with pytest.raises(WorkflowError):
        ex.execute(hop.workflow, job, {"task": "recipe app for families"})
