"""Workflow engine tests — retries/backoff, in-engine FIX loop, timing,
artifact dedupe/refresh, and the universal respond workflow.

Hermetic: no GEMINI_API_KEY anywhere. Model-or-fail doctrine — tests
inject fake models via monkeypatch; without one, jobs fail loud.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["GEMINI_API_KEY"] = ""

import pytest

from nine.gates.evidence import (
    EvidenceGate,
    required_artifact_check,
)
from nine.ledger.ledger import JSONLLedger
from nine.router.classifier import KeywordRouter, Router
from nine.runtime.responder import respond_gate, respond_workflow
from nine.runtime.workflows import Node, Workflow, WorkflowError, WorkflowExecutor


def _gate(*checks):
    g = EvidenceGate()
    for name, check in checks:
        g.register_check(name, check)
    return g


def _run(wf, gate, job, tmp_path, **kw):
    ex = WorkflowExecutor(JSONLLedger(tmp_path / "ledger.jsonl"), gate,
                          workdir=tmp_path / "work")
    return ex.execute(wf, job, {"task": "t"}, **kw)


# ---------------------------------------------------------------- node timeout env override
def test_node_timeout_env_override_applies(monkeypatch):
    monkeypatch.setenv("NINE_NODE_TIMEOUT_S", "90")
    n = Node(id="n", kind="bash", command="true")
    assert n.timeout_seconds == 90


def test_node_timeout_env_malformed_keeps_default(monkeypatch):
    monkeypatch.setenv("NINE_NODE_TIMEOUT_S", "banana")
    n = Node(id="n", kind="bash", command="true")
    assert n.timeout_seconds == 300


def test_node_timeout_env_ignored_when_node_is_infinite(monkeypatch):
    monkeypatch.setenv("NINE_NODE_TIMEOUT_S", "90")
    n = Node(id="n", kind="bash", command="true", timeout_seconds=None)
    assert n.timeout_seconds is None


def test_node_timeout_env_unset_keeps_node_default(monkeypatch):
    monkeypatch.delenv("NINE_NODE_TIMEOUT_S", raising=False)
    n = Node(id="n", kind="bash", command="true")
    assert n.timeout_seconds == 300


# ---------------------------------------------------------------- retries

def test_node_retries_on_exception_then_succeeds(tmp_path):
    calls = {"n": 0}

    def flaky(inputs, job_dir):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("transient")
        (job_dir / "out.txt").write_text("done\n")
        return {"output": "ok"}

    wf = Workflow(id="w")
    wf.add_node(Node(id="x", kind="tool", run=flaky,
                     max_retries=2, retry_delay_seconds=0.01))
    job = JSONLLedger(tmp_path / "ledger.jsonl").submit("w", {"task": "t"})
    res = _run(wf, _gate(("a", required_artifact_check(["out.txt"]))), job, tmp_path)
    assert res["verdict"]["verdict"] == "SHIP"
    assert calls["n"] == 3
    assert res["node_meta"]["x"]["attempts"] == 3


def test_bash_retry_on_nonzero_exit(tmp_path):
    wf = Workflow(id="w")
    wf.add_node(Node(id="x", kind="bash", command="exit 1",
                     max_retries=2, retry_delay_seconds=0.01, retry_on_exit=True))
    job = JSONLLedger(tmp_path / "ledger.jsonl").submit("w", {"task": "t"})
    res = _run(wf, _gate(("a", required_artifact_check(["out.txt"]))), job, tmp_path)
    assert res["node_meta"]["x"]["attempts"] == 3
    assert res["node_outputs"]["x"]["exit_code"] == 1


def test_node_retries_exhausted_raises(tmp_path):
    def always_boom(inputs, job_dir):
        raise RuntimeError("kaput")

    wf = Workflow(id="w")
    wf.add_node(Node(id="x", kind="tool", run=always_boom,
                     max_retries=1, retry_delay_seconds=0.01))
    job = JSONLLedger(tmp_path / "ledger.jsonl").submit("w", {"task": "t"})
    with pytest.raises(WorkflowError):
        _run(wf, _gate(("a", required_artifact_check(["out.txt"]))), job, tmp_path)
    assert job.status == "failed"


# ------------------------------------------------------------ in-engine FIX

def test_fix_loop_reruns_with_directive_and_ships(tmp_path):
    """First run fails the gate (missing artifact); the in-engine FIX loop
    reruns with fix_directive and the node produces the artifact -> SHIP."""
    runs = {"n": 0}

    def delayed_write(inputs, job_dir):
        runs["n"] += 1
        if inputs.get("fix_directive"):
            (job_dir / "FIXED.md").write_text("fixed\n")
        return {"output": f"run {runs['n']}"}

    wf = Workflow(id="w")
    wf.add_node(Node(id="make", kind="tool", run=delayed_write))
    job = JSONLLedger(tmp_path / "ledger.jsonl").submit("w", {"task": "t"})
    res = _run(wf, _gate(("a", required_artifact_check(["FIXED.md"]))), job, tmp_path)
    assert res["verdict"]["verdict"] == "SHIP"
    assert res["attempts"] == 2
    assert runs["n"] == 2
    rec = JSONLLedger(tmp_path / "ledger.jsonl").get(job.job_id)
    assert rec.status == "shipped"
    assert len(rec.verdicts) == 2
    assert rec.verdicts[0]["verdict"] == "FIX"
    assert rec.verdicts[1]["verdict"] == "SHIP"


def test_fix_loop_false_single_pass_blocks(tmp_path):
    def never_write(inputs, job_dir):
        return {"output": "nothing"}

    wf = Workflow(id="w")
    wf.add_node(Node(id="make", kind="tool", run=never_write))
    job = JSONLLedger(tmp_path / "ledger.jsonl").submit("w", {"task": "t"})
    res = _run(wf, _gate(("a", required_artifact_check(["FIXED.md"]))), job, tmp_path,
               fix_loop=False)
    assert res["verdict"]["verdict"] == "FIX"   # gate FIX -> engine blocks
    assert res["attempts"] == 1
    assert JSONLLedger(tmp_path / "ledger.jsonl").get(job.job_id).status == "blocked"


def test_fix_loop_exhausts_to_blocked(tmp_path):
    wf = Workflow(id="w")
    wf.add_node(Node(id="make", kind="bash", command="echo hi"))
    job = JSONLLedger(tmp_path / "ledger.jsonl").submit("w", {"task": "t"})
    job.max_fix_loops = 1
    res = _run(wf, _gate(("a", required_artifact_check(["FIXED.md"]))), job, tmp_path)
    assert res["verdict"]["verdict"] == "FIX"   # last FIX -> engine blocks
    assert res["attempts"] == 2
    assert JSONLLedger(tmp_path / "ledger.jsonl").get(job.job_id).status == "blocked"


def test_artifact_sha_refreshes_on_fix_rerun(tmp_path):
    """A FIX rerun that rewrites a file refreshes its sha256; the manifest
    keeps ONE entry per name (latest content)."""
    import hashlib
    runs = {"n": 0}

    def writer(inputs, job_dir):
        runs["n"] += 1
        content = "A" if runs["n"] == 1 else "BBBB"
        (job_dir / "out.txt").write_text(content + "\n")
        return {"output": content}

    wf = Workflow(id="w")
    wf.add_node(Node(id="w", kind="tool", run=writer))
    # gate passes only when content is 'BBBB' (via a size-sensing check)
    def big_enough(ctx, workdir):
        f = Path(workdir) / "out.txt"
        return (f.exists() and f.stat().st_size >= 5), "size ok"
    big_enough.expected = ["out.txt"]  # type: ignore[attr-defined]  # torture-17 F2 tag

    job = JSONLLedger(tmp_path / "ledger.jsonl").submit("w", {"task": "t"})
    res = _run(wf, _gate(("big", big_enough)), job, tmp_path)
    assert res["verdict"]["verdict"] == "SHIP"
    assert res["attempts"] == 2
    arts = [a for a in res["artifacts"] if a["name"] == "out.txt"]
    assert len(arts) == 1
    expect = hashlib.sha256(b"BBBB\n").hexdigest()
    assert arts[0]["sha256"] == expect


def test_node_timing_meta_recorded(tmp_path):
    wf = Workflow(id="w")
    wf.add_node(Node(id="x", kind="bash", command="echo hi > out.txt"))
    job = JSONLLedger(tmp_path / "ledger.jsonl").submit("w", {"task": "t"})
    res = _run(wf, _gate(("a", required_artifact_check(["out.txt"]))), job, tmp_path)
    meta = res["node_meta"]["x"]
    assert meta["attempts"] >= 1
    assert meta["duration_ms"] >= 0
    rec = JSONLLedger(tmp_path / "ledger.jsonl").get(job.job_id)
    assert rec.metadata["nodes"]["x"]["duration_ms"] >= 0


# -------------------------------------------------------- universal respond

def test_respond_workflow_fails_loud_without_model(tmp_path):
    """No offline fallback: without a model the respond job must fail loud
    (WorkflowError), never produce a canned answer."""
    wf = respond_workflow()
    job = JSONLLedger(tmp_path / "ledger.jsonl").submit("respond", {"task": "hello there"})
    with pytest.raises(WorkflowError):
        _run(wf, respond_gate(), job, tmp_path)
    assert job.status == "failed"
    assert not (tmp_path / "work" / job.job_id / "RESPONSE.md").exists()


def test_respond_workflow_uses_model_answer(tmp_path, monkeypatch):
    from nine.runtime import responder

    monkeypatch.setattr(responder, "respond_text",
                        lambda task, max_chars=600: ("a real model answer", "gemini"))
    wf = respond_workflow()
    job = JSONLLedger(tmp_path / "ledger.jsonl").submit("respond", {"task": "hi"})
    res = _run(wf, respond_gate(), job, tmp_path)
    assert res["verdict"]["verdict"] == "SHIP"
    resp = (tmp_path / "work" / job.job_id / "RESPONSE.md").read_text()
    assert "a real model answer" in resp


def test_router_falls_back_to_respond():
    kr = KeywordRouter()
    wf_id, conf, _ = kr.classify("zzz qqq totally unknown")
    assert wf_id == "respond"
    assert conf == 0.0
    r = Router(workflows={})
    dec = r.classify("zzz qqq totally unknown")
    assert dec.workflow_id == "respond"


def test_respond_gate_blocks_empty_response(tmp_path):
    wf = Workflow(id="respond")
    wf.add_node(Node(id="r", kind="bash", command="echo '' > RESPONSE.md"))
    job = JSONLLedger(tmp_path / "ledger.jsonl").submit("respond", {"task": "x"})
    res = _run(wf, respond_gate(), job, tmp_path)
    assert res["verdict"]["verdict"] == "FIX"  # empty response cannot SHIP
    assert JSONLLedger(tmp_path / "ledger.jsonl").get(job.job_id).status == "blocked"
