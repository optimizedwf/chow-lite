
"""Chain engine tests — 5-hop flagship chain + demo lane (no API key needed).

Hermetic: no GEMINI_API_KEY anywhere. Model-or-fail doctrine — model-backed
hops (summarize / ADK build / Gemma teach) run with monkeypatched fakes;
without one they fail loud.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json

from nine.chains.chain import Chain, ChainError, ChainExecutor, Hop
from nine.chains.flagship import demo_lane, research_plan_build_review_teach
from nine.gates.evidence import (
    EvidenceGate,
    eval_json_check,
    exit_codes_check,
    required_artifact_check,
)
from nine.ledger.ledger import JSONLLedger
from nine.runtime.workflows import Node, Workflow, WorkflowError, WorkflowExecutor


def _install_fake_models(monkeypatch) -> None:
    """Inject fake models for the model-backed hops (see test_memory.py)."""
    from nine.chains import flagship
    from nine.runtime import summarizer

    monkeypatch.setattr(
        summarizer, "summarize_text",
        lambda text, max_words=120, task="", api_key=None:
        ("distilled findings about fooquark", "fake-gemini"),
    )

    def fake_build_run(inputs, job_dir):
        (Path(job_dir) / "solution.py").write_text(
            "def answer():\n    return 42\n", encoding="utf-8")
        return {"output": "wrote solution.py"}

    monkeypatch.setattr(
        flagship, "_build_adk_node",
        lambda: Node(id="build", kind="tool", run=fake_build_run,
                     description="fake ADK node (hermetic test)"),
    )
    monkeypatch.setattr(
        "nine.runtime.gemma.gemma_generate",
        lambda prompt, model=None, api_key=None, timeout=90:
        "gate every hop on evidence before handoff.",
    )


def test_flagship_chain_ships_all_hops(tmp_path, monkeypatch):
    _install_fake_models(monkeypatch)
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = ChainExecutor(ledger, workdir=tmp_path / "work")

    job = ledger.submit("research-plan-build-review-teach", {"task": "build a calculator"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("build a calculator\n")

    res = ex.execute(research_plan_build_review_teach(), job, {"task": "build a calculator"})
    assert res["final"] == "SHIPPED"
    assert all(info["verdict"] == "SHIP" for info in res["hop_results"].values())
    names = {a["name"] for a in ledger.get(job.job_id).artifacts}
    assert {"research.md", "PLAN.md", "EVAL.json", "review.md", "TEACH.md"} <= names


def test_demo_lane_ships(tmp_path):
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = ChainExecutor(ledger, workdir=tmp_path / "work")

    job = ledger.submit("inbox-triage-task-report", {"task": "inbox item"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "inbox.txt").write_text("customer refund question\n")

    res = ex.execute(demo_lane(), job, {"task": "inbox item"})
    assert res["final"] == "SHIPPED"
    names = {a["name"] for a in ledger.get(job.job_id).artifacts}
    assert {"triage.md", "task_result.md", "EVAL.json", "FINAL_REPORT.md"} <= names


def test_chain_blocks_when_gate_fails(tmp_path):
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = ChainExecutor(ledger, workdir=tmp_path / "work")

    bad_wf = Workflow(id="bad")
    bad_wf.add_node(Node(id="bad", kind="bash", command="echo 'no artifact'"))
    chain = Chain(
        id="test-block",
        hops=[Hop(id="bad", workflow=bad_wf, required_artifacts=["NEVER.md"],
                  gate_checks={"need": required_artifact_check(["NEVER.md"])},
                  max_fix_loops=1)],
    )
    job = ledger.submit("test-block", {"task": "x"})
    res = ex.execute(chain, job, {"task": "x"})
    assert res["final"] == "BLOCKED"
    assert res["at_hop"] == "bad"


def test_unknown_hop_raises(tmp_path):
    chain = research_plan_build_review_teach()
    with pytest.raises(ChainError):
        chain.hop("nope")


def test_chain_job_reaches_terminal_state(tmp_path):
    """Chain job must leave 'submitted' and end SHIPPED in the durable ledger."""
    from nine.chains.flagship import demo_lane
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = ChainExecutor(ledger, workdir=tmp_path / "work")

    job = ledger.submit("inbox-triage-task-report", {"task": "inbox item"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "inbox.txt").write_text("customer refund question\n")

    res = ex.execute(demo_lane(), job, {"task": "inbox item"})
    assert res["final"] == "SHIPPED"
    # durable ledger reflects the terminal state (was stuck at 'submitted')
    assert ledger.get(job.job_id).status == "shipped"


def test_chain_crash_marks_job_failed(tmp_path):
    """A crashing hop must mark the chain job 'failed', not leave it dangling."""
    from nine.chains.chain import Chain, ChainExecutor, Hop

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = ChainExecutor(ledger, workdir=tmp_path / "work")

    def boom(inputs, job_dir):
        raise RuntimeError("simulated crash")

    wf = Workflow(id="boom")
    wf.add_node(Node(id="x", kind="tool", run=boom))
    chain = Chain(id="boom-chain", hops=[Hop(id="hop1", workflow=wf)])

    job = ledger.submit("boom-chain", {"task": "t"})
    try:
        ex.execute(chain, job, {"task": "t"})
        raise AssertionError("expected ChainError")
    except Exception:  # noqa: BLE001 - deliberately broad; we assert on state
        pass
    assert ledger.get(job.job_id).status == "failed"


def test_fix_loop_retries_on_failed_check_not_just_missing_artifact(tmp_path):
    """A gate FIX from a failing EVAL check (no missing artifacts) must re-run."""
    from nine.chains.chain import Chain, ChainExecutor, Hop
    from nine.gates.evidence import eval_json_check

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = ChainExecutor(ledger, workdir=tmp_path / "work")
    calls = {"n": 0}

    def flaky(inputs, job_dir):
        calls["n"] += 1
        (job_dir / "FINAL_REPORT.md").write_text("report\n")
        # EVAL.json passes only on the 2nd attempt
        ok = calls["n"] >= 2
        (job_dir / "EVAL.json").write_text(
            '{"checks":[{"name":"r","passed":%s}]}' % ("true" if ok else "false"))
        return {"stdout": "done"}

    wf = Workflow(id="flaky")
    wf.add_node(Node(id="make", kind="tool", run=flaky))
    gate = {"eval-json": eval_json_check(), "artifacts": required_artifact_check(["FINAL_REPORT.md"])}
    chain = Chain(id="flaky-chain", hops=[Hop(id="hop1", workflow=wf, gate_checks=gate, max_fix_loops=2)])

    job = ledger.submit("flaky-chain", {"task": "t"})
    res = ex.execute(chain, job, {"task": "t"})
    assert res["final"] == "SHIPPED"
    assert calls["n"] == 2  # first attempt failed the gate, second shipped


def test_registry_dispatch_produces_distinct_artifacts(tmp_path, monkeypatch):
    """research/build/review must produce DIFFERENT artifacts (P1-3 fix:
    previously every workflow_id produced byte-identical output)."""
    _install_fake_models(monkeypatch)
    from nine.registry import WORKFLOWS
    from nine.runtime.workflows import WorkflowExecutor

    results = {}
    for wf_id in ("research", "build", "review"):
        ledger = JSONLLedger(tmp_path / f"{wf_id}.jsonl")
        gate = EvidenceGate()
        gate.register_check("eval-json", eval_json_check())
        gate.register_check("exit-codes", exit_codes_check())
        ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / f"w-{wf_id}")
        job = ledger.submit(wf_id, {"task": f"do {wf_id}"})
        job_dir = tmp_path / f"w-{wf_id}" / job.job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "task.txt").write_text(f"do {wf_id}\n")
        ex.execute(WORKFLOWS[wf_id](), job, {"task": f"do {wf_id}"})
        results[wf_id] = {a["name"] for a in ledger.get(job.job_id).artifacts}
    assert "research.md" in results["research"]
    assert "solution.py" in results["build"] and "EVAL.json" in results["build"]
    assert "review.md" in results["review"]
    assert results["research"] != results["build"] != results["review"]


def test_adk_build_hop_fails_loud_without_model(tmp_path, monkeypatch):
    """No offline fallback: without GEMINI_API_KEY the build hop raises
    WorkflowError — never a canned solution.py ("return 42")."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    from nine.chains.flagship import build_hop

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    gate = EvidenceGate()
    gate.register_check("eval-json", eval_json_check())
    gate.register_check("exit-codes", exit_codes_check())
    gate.register_check("artifacts", required_artifact_check(["solution.py", "EVAL.json"]))
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("build", {"task": "build a tiny thing"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("build a tiny thing\n")

    with pytest.raises(WorkflowError):
        ex.execute(build_hop().workflow, job, {"task": "build a tiny thing"})
    assert job.status == "failed"
    assert not (job_dir / "solution.py").exists()


def test_adk_build_hop_ships_with_fake_model_and_independent_eval(tmp_path, monkeypatch):
    """With a model, the build hop SHIPs and the INDEPENDENT self-test node
    writes EVAL.json from the ACTUAL run result (builder never self-certifies)."""
    _install_fake_models(monkeypatch)
    from nine.chains.flagship import build_hop

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    gate = EvidenceGate()
    gate.register_check("eval-json", eval_json_check())
    gate.register_check("exit-codes", exit_codes_check())
    gate.register_check("artifacts", required_artifact_check(["solution.py", "EVAL.json"]))
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("build", {"task": "build a tiny thing"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("build a tiny thing\n")

    res = ex.execute(build_hop().workflow, job, {"task": "build a tiny thing"})
    assert res["verdict"]["verdict"] == "SHIP"
    # EVAL.json was written by the self-test node, not the builder
    ev = json.loads((job_dir / "EVAL.json").read_text())
    assert ev["checks"][0]["name"] == "solution-runs"
    assert ev["checks"][0]["passed"] is True
    assert (job_dir / "build.log").exists()
