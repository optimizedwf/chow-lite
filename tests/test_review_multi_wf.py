"""review-multi workflow tests - hermetic, model-or-fail.

Tests inject fake ADK reviewer/merger nodes via monkeypatch; without
GEMINI_API_KEY the real nodes fail loud (WorkflowError).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

os.environ["GEMINI_API_KEY"] = ""

from nine.chains.chain import Chain, ChainExecutor
from nine.gates.evidence import (
    EvidenceGate,
)
from nine.ledger.ledger import JSONLLedger
from nine.runtime.workflows import Node, WorkflowError, WorkflowExecutor
from nine.workflows.review_multi_wf import _DIM_FILES, review_multi_hop


def _fake_reviewer_run(dimension: str, filename: str):
    def fake_run(inputs, job_dir):
        job_dir = Path(job_dir)
        out = job_dir / "reviews" / filename
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            f"## Verdict: PASS\n"
            f"## Findings\n- none\n"
            f"## Summary\n{dimension} looks clean.\n",
            encoding="utf-8")
        return {"output": f"wrote reviews/{filename}"}
    return fake_run


def _install_fake_reviewers(monkeypatch):
    """Replace all 4 reviewer nodes + merger with hermetic ones."""
    from nine.workflows import review_multi_wf

    def fake_factory(dimension: str, filename: str) -> Node:
        return Node(id=f"{dimension}-review", kind="tool",
                    run=_fake_reviewer_run(dimension, filename),
                    description="fake reviewer (hermetic)")

    monkeypatch.setattr(review_multi_wf, "_reviewer_adk_node", fake_factory)

    def fake_merge(inputs, job_dir):
        job_dir = Path(job_dir)
        reports = []
        for rel in _DIM_FILES:
            p = job_dir / rel
            if p.exists():
                reports.append(p.read_text(encoding="utf-8")[:500])
        (job_dir / "REVIEW.md").write_text(
            "# Review\n"
            "## Overall Verdict: PASS\n"
            "## Consolidated Findings\n- none\n"
            "## Per-Dimension Summaries\n" + "\n".join(reports),
            encoding="utf-8")
        return {"output": "wrote REVIEW.md"}

    monkeypatch.setattr(review_multi_wf, "_merge_adk_node",
                        lambda: Node(id="merge", kind="tool", run=fake_merge,
                                     description="fake merger (hermetic)"))


def _make_gate(hop):
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)
    return gate


def test_review_multi_ships_with_all_reports(tmp_path, monkeypatch):
    """All 4 per-dim reports + merged REVIEW.md with verdict -> SHIP."""
    _install_fake_reviewers(monkeypatch)
    hop = review_multi_hop()
    gate = _make_gate(hop)

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("review-multi", {"task": "review the calculator"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "solution.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")

    res = ex.execute(hop.workflow, job, {"task": "review the calculator"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert (job_dir / "REVIEW.md").exists()
    for rel in _DIM_FILES:
        assert (job_dir / rel).exists(), f"missing {rel}"
    assert "Overall Verdict: PASS" in (job_dir / "REVIEW.md").read_text()


def test_review_multi_merge_retries_when_verdict_missing(tmp_path, monkeypatch):
    """Merger without Verdict line -> FIX; retry writes verdict -> SHIP."""
    from nine.workflows import review_multi_wf

    def fake_factory(dimension: str, filename: str) -> Node:
        return Node(id=f"{dimension}-review", kind="tool",
                    run=_fake_reviewer_run(dimension, filename),
                    description="fake reviewer (hermetic)")
    monkeypatch.setattr(review_multi_wf, "_reviewer_adk_node", fake_factory)

    state = {"calls": 0}

    def fake_merge(inputs, job_dir):
        job_dir = Path(job_dir)
        state["calls"] += 1
        fix_dir = inputs.get("fix_directive", "")
        if state["calls"] == 1 and not fix_dir:
            (job_dir / "REVIEW.md").write_text(
                "# Review\nNo verdict here.\n", encoding="utf-8")
        else:
            (job_dir / "REVIEW.md").write_text(
                "# Review\n## Overall Verdict: PASS\n",
                encoding="utf-8")
        return {"output": "wrote REVIEW.md"}

    monkeypatch.setattr(review_multi_wf, "_merge_adk_node",
                        lambda: Node(id="merge", kind="tool", run=fake_merge,
                                     description="fake merger (hermetic)"))

    hop = review_multi_hop()
    gate = _make_gate(hop)
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("review-multi", {"task": "review the calculator"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "solution.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")

    res = ex.execute(hop.workflow, job, {"task": "review the calculator"})
    assert state["calls"] >= 2, "merge should have retried"
    assert res["verdict"]["verdict"] == "SHIP"
    assert "Overall Verdict: PASS" in (job_dir / "REVIEW.md").read_text()


def test_review_multi_fails_loud_without_api_key(tmp_path):
    """Without GEMINI_API_KEY the real reviewer node raises WorkflowError."""
    hop = review_multi_hop()
    gate = _make_gate(hop)

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("review-multi", {"task": "review the calculator"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)

    with pytest.raises(WorkflowError):
        ex.execute(hop.workflow, job, {"task": "review the calculator"})


def test_review_multi_in_chain(tmp_path, monkeypatch):
    """review-multi chains after build: build -> review-multi -> SHIPPED."""
    _install_fake_reviewers(monkeypatch)
    from nine.chains import flagship

    def fake_build_run(inputs, job_dir):
        (Path(job_dir) / "solution.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8")
        return {"output": "wrote solution.py"}

    monkeypatch.setattr(
        flagship, "_build_adk_node",
        lambda: Node(id="build", kind="tool", run=fake_build_run,
                     description="fake ADK (hermetic)"),
    )

    from nine.chains.flagship import build_hop
    chain = Chain(
        id="build-review-multi",
        hops=[build_hop(), review_multi_hop()],
        description="Build then 4-dimensional review",
    )
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = ChainExecutor(ledger, workdir=tmp_path / "work")
    job = ledger.submit("build-review-multi", {"task": "build a calculator"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "task.txt").write_text("build a calculator\n")

    res = ex.execute(chain, job, {"task": "build a calculator"})
    assert res["final"] == "SHIPPED"
    assert (job_dir / "solution.py").exists()
    assert (job_dir / "REVIEW.md").exists()
    assert (job_dir / "reviews" / "security.md").exists()
