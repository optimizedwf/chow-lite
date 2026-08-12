"""Chain engine tests — 5-hop flagship chain + demo lane (no API key needed)."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chowlite.chains.chain import Chain, ChainError, ChainExecutor, Hop
from chowlite.chains.flagship import demo_lane, research_plan_build_review_teach
from chowlite.gates.evidence import required_artifact_check
from chowlite.ledger.ledger import JSONLLedger
from chowlite.runtime.workflows import Node, Workflow


def test_flagship_chain_ships_all_hops(tmp_path):
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
