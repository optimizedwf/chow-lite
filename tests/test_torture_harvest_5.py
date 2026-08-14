"""Torture harvest 5 — round-4 findings (torture-7 + torture-8) regression tests.

torture-8 (runtime deep edges): symlink manifest registration, recover
symlink-wipe refusal, cancel control-plane honesty, callable-timeout retry +
timeout_seconds validation, bash process-group kill, recover --force, learn
store byte tolerance, doc-truth sweep.
torture-7 (chains/gates/plugins/server): stale-EVAL SHIP guard, flagship
fix_directive, compose plugin id collision, honest chain route decision,
recover chain loud-fail, chunked-body cap, hop_artifacts forwarding.
"""
import hashlib
import json
import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["GEMINI_API_KEY"] = ""

from nine.gates.evidence import EvidenceGate, eval_json_check, required_artifact_check
from nine.ledger.ledger import JSONLLedger
from nine.runtime.workflows import (
    Node,
    NodeTimeoutError,
    Workflow,
    WorkflowError,
    WorkflowExecutor,
)

_ALWAYS_TRUE = lambda ctx, wd: (True, "ok")  # noqa: E731


def _gate():
    g = EvidenceGate()
    g.register_check("always", _ALWAYS_TRUE)
    return g


# ---------------------------------------------------------------- T8-F1
def test_manifest_never_registers_symlinked_real_file(tmp_path):
    """A symlink to a REAL outside file must never appear in the manifest
    (and never with the outside sha256)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data.txt").write_text("secret-outside-content", encoding="utf-8")
    outside_sha = hashlib.sha256((outside / "data.txt").read_bytes()).hexdigest()

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, _gate(), workdir=tmp_path / "work")
    job = ledger.submit("sym", {"task": "x"})
    wf = Workflow(id="sym")
    wf.add_node(Node(id="b1", kind="bash",
                     command=f"echo real > real.txt; ln -sf {outside / 'data.txt'} data.txt"))
    res = ex.execute(wf, job, {"task": "x"})
    names = {a["name"] for a in res["artifacts"]}
    assert "real.txt" in names
    assert "data.txt" not in names
    assert all(a.get("sha256") != outside_sha for a in res["artifacts"])


def test_explicit_artifact_path_symlink_skipped(tmp_path):
    """A tool node returning a symlink as its artifact path must be skipped."""
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")

    def _run(inputs, job_dir):
        target = Path(job_dir) / "linked.txt"
        if not target.exists():
            target.symlink_to(outside)
        return {"output": "ok", "artifact_path": str(target)}

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, _gate(), workdir=tmp_path / "work")
    job = ledger.submit("symart", {"task": "x"})
    wf = Workflow(id="symart")
    wf.add_node(Node(id="t1", kind="tool", run=_run))
    res = ex.execute(wf, job, {"task": "x"})
    names = {a["name"] for a in res["artifacts"]}
    assert "linked.txt" not in names


# ---------------------------------------------------------------- T8-F2
def test_recover_refuses_symlinked_job_dir(tmp_path, monkeypatch):
    """recover must refuse (and never wipe) a job_dir that is a symlink."""
    from nine import cli

    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / "important.txt").write_text("keep me", encoding="utf-8")
    (victim / "sub").mkdir()
    (victim / "sub" / "nested.txt").write_text("nested", encoding="utf-8")

    ledger_path = tmp_path / "ledger.jsonl"
    ledger = JSONLLedger(ledger_path)
    job = ledger.submit("respond", {"task": "hello"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.parent.mkdir(parents=True, exist_ok=True)
    job_dir.symlink_to(victim, target_is_directory=True)
    (victim / "task.txt").write_text("hello\n", encoding="utf-8")

    monkeypatch.setattr(cli, "_ledger", lambda args: ledger)
    args = type("A", (), {"job_id": job.job_id, "workdir": tmp_path / "work",
                          "force": False})()
    rc = cli.cmd_recover(args)
    assert rc == 1
    # victim untouched
    assert (victim / "important.txt").read_text() == "keep me"
    assert (victim / "sub" / "nested.txt").read_text() == "nested"


# ---------------------------------------------------------------- T8-F3
def test_cancel_cross_process_stops_executor_and_never_ships(tmp_path):
    """A cancel appended by another process must stop the run: final durable
    status is cancelled, no shipped line is appended."""
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")

    def slow_run(inputs, job_dir):
        time.sleep(1.5)
        (Path(job_dir) / "OUT.txt").write_text("late", encoding="utf-8")
        return {"output": "done"}

    ex = WorkflowExecutor(ledger, _gate(), workdir=tmp_path / "work")
    job = ledger.submit("slow", {"task": "x"})
    wf = Workflow(id="slow")
    wf.add_node(Node(id="t1", kind="tool", run=slow_run, max_retries=0))
    # cancel from a SECOND ledger instance (cross-process shape)
    other = JSONLLedger(tmp_path / "ledger.jsonl")
    import threading
    t = threading.Timer(0.3, lambda: other.cancel(job.job_id))
    t.start()
    res = ex.execute(wf, job, {"task": "x"})
    t.join()
    assert res["verdict"]["verdict"] == "CANCELLED"
    assert job.status == "cancelled"
    lines = [json.loads(ln) for ln in
             (tmp_path / "ledger.jsonl").read_text().splitlines() if ln.strip()]
    final = [r for r in lines if r["job_id"] == job.job_id][-1]
    assert final["status"] == "cancelled"
    assert "shipped" not in [r["status"] for r in lines if r["job_id"] == job.job_id]


def test_chain_stops_on_cancel_between_hops(tmp_path):
    """A cancel mid-chain must stop between hops (container ends cancelled)."""
    from nine.chains.chain import Chain, ChainExecutor, Hop

    def mk_hop(hid):
        wf = Workflow(id=hid)
        wf.add_node(Node(id="b1", kind="bash",
                         command=f"echo {hid} > {hid}.txt; sleep 1.2; "
                                 f"printf '{{\"checks\":[{{\"name\":\"c\",\"passed\":true}}]}}' > EVAL.json"))
        return Hop(id=hid, workflow=wf,
                   required_artifacts=[f"{hid}.txt", "EVAL.json"],
                   gate_checks={"eval-json": eval_json_check()})

    chain = Chain(id="cc", hops=[mk_hop("h1"), mk_hop("h2")])
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    cex = ChainExecutor(ledger, workdir=tmp_path / "work")
    job = ledger.submit("cc", {"task": "x"})
    other = JSONLLedger(tmp_path / "ledger.jsonl")
    import threading
    t = threading.Timer(0.2, lambda: other.cancel(job.job_id))
    t.start()
    res = cex.execute(chain, job, {"task": "x"})
    t.join()
    assert res["final"] == "CANCELLED"
    assert job.status == "cancelled"


# ---------------------------------------------------------------- T8-F4
def test_callable_timeout_is_retried_and_can_succeed(tmp_path):
    """A callable-node timeout must be retried per max_retries (was never
    retried — WorkflowError was classified deterministic)."""
    calls = {"n": 0}

    def flaky(inputs, job_dir):
        calls["n"] += 1
        if calls["n"] < 3:
            time.sleep(0.3)  # exceeds the 0.05 deadline -> NodeTimeoutError
        return {"output": "finally"}

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, _gate(), workdir=tmp_path / "work")
    job = ledger.submit("retry", {"task": "x"})
    wf = Workflow(id="retry")
    wf.add_node(Node(id="t1", kind="tool", run=flaky,
                     timeout_seconds=1, max_retries=3, retry_delay_seconds=0.01))
    # deadline 1s is too long for a 0.3s sleep; use a short deadline instead
    wf.nodes["t1"].timeout_seconds = 0.05
    res = ex.execute(wf, job, {"task": "x"})
    assert calls["n"] >= 3  # attempt 1 + retries
    assert res["verdict"]["verdict"] == "SHIP"


def test_callable_timeout_exhausts_into_blocked(tmp_path):
    """Always-timeouting callable with max_retries=0 fails loud."""
    def always(inputs, job_dir):
        time.sleep(0.4)

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, _gate(), workdir=tmp_path / "work")
    job = ledger.submit("timeout", {"task": "x"})
    wf = Workflow(id="timeout")
    wf.add_node(Node(id="t1", kind="tool", run=always,
                     timeout_seconds=1, max_retries=0))
    wf.nodes["t1"].timeout_seconds = 0.05  # post-init override (float bypasses guard)
    with pytest.raises(WorkflowError, match="failed after"):
        ex.execute(wf, job, {"task": "x"})
    assert job.status == "failed"
    # abandoned worker is recorded for operator visibility (T6-F5)
    assert job.metadata.get("timeout_abandoned_worker", {}).get("node") == "t1"


def test_timeout_seconds_zero_rejected_at_construction():
    """timeout_seconds=0 or negative must fail loudly at Node construction."""
    with pytest.raises(ValueError, match="timeout_seconds"):
        Node(id="x", kind="bash", command="echo hi", timeout_seconds=0)
    with pytest.raises(ValueError, match="timeout_seconds"):
        Node(id="x", kind="bash", command="echo hi", timeout_seconds=-3)
    # None = wait forever (documented) — still constructible
    n = Node(id="x", kind="bash", command="echo hi", timeout_seconds=None)
    assert n.timeout_seconds is None


def test_node_timeout_error_is_distinct_and_retryable():

    assert issubclass(NodeTimeoutError, Exception)
    assert not issubclass(NodeTimeoutError, WorkflowError)


# ---------------------------------------------------------------- T8-F5
def test_bash_timeout_kills_process_group_no_ghost(tmp_path):
    """A bash node that spawns a background writer must have that writer
    killed on timeout — no ghost file lands after the job failed."""
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, _gate(), workdir=tmp_path / "work")
    job = ledger.submit("bashghost", {"task": "x"})
    wf = Workflow(id="bashghost")
    wf.add_node(Node(id="b1", kind="bash",
                     command="(sleep 1.2; echo GHOST > GHOST.txt) & sleep 30",
                     timeout_seconds=1, max_retries=0))
    start = time.monotonic()
    with pytest.raises(WorkflowError, match="failed after"):
        ex.execute(wf, job, {"task": "x"})
    elapsed = time.monotonic() - start
    # give any orphan a chance to write, then verify nothing appeared
    time.sleep(1.5)
    job_dir = tmp_path / "work" / job.job_id
    assert not (job_dir / "GHOST.txt").exists()
    # no sleep-30 process left behind
    assert elapsed < 8


# ---------------------------------------------------------------- T8-F6
def test_recover_force_rescues_stale_running_job(tmp_path, monkeypatch):
    """A job stuck at 'running' (crash) must be recoverable with --force."""
    from nine import cli

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    job = ledger.submit("respond", {"task": "hello world task"})
    # simulate a crash: force the durable status to running
    job.status = "running"
    ledger.update(job)
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("hello world task\n", encoding="utf-8")

    # respond requires a key -> recover --force must degrade to failed and
    # then _execute_job fails LOUD with WorkflowError -> clean exit 1
    monkeypatch.setattr(cli, "_ledger", lambda args: ledger)
    args = type("A", (), {"job_id": job.job_id, "workdir": tmp_path / "work",
                          "force": True})()
    rc = cli.cmd_recover(args)
    assert rc == 1
    # job left in a terminal recoverable state (failed or blocked), NOT running
    assert ledger.refresh(job.job_id).status in ("failed", "blocked")


# ---------------------------------------------------------------- T8-F7
def test_learn_stores_tolerate_non_utf8_byte(tmp_path):
    """events.jsonl / candidates.jsonl with one corrupt byte must degrade
    (healthy records intact) instead of bricking learn commands."""
    from nine.learn.learner import (
        CandidateStore,
        ImprovementCandidate,
        RouteEvent,
        RouteEventStore,
    )

    ev = RouteEventStore(tmp_path / "events.jsonl")
    ev.record(RouteEvent(event_id="ev-1", job_id="j1", task_redacted="t",
                         workflow_id="respond", confidence=1.0,
                         router_version="0.1.0", verdict="SHIP",
                         checks_passed=1, checks_total=1))
    raw = (tmp_path / "events.jsonl").read_bytes() + b"\xff\xfe\n"
    (tmp_path / "events.jsonl").write_bytes(raw)
    events = ev.all()
    assert len(events) == 1
    assert events[0].workflow_id == "respond"

    cand = CandidateStore(tmp_path / "candidates.jsonl")
    cand.append(ImprovementCandidate(
        candidate_id="c1", kind="keyword",
        description="add hello", evidence=["ev-1"],
        params={"workflow_id": "respond", "keyword": "hello"}))
    raw2 = (tmp_path / "candidates.jsonl").read_bytes() + b"\xff\n"
    (tmp_path / "candidates.jsonl").write_bytes(raw2)
    cands = cand.all()
    assert len(cands) == 1
    assert cands[0].candidate_id == "c1"


def test_memory_graph_len_tolerates_non_utf8(tmp_path):
    from nine.memory.graph import LocalMemoryGraph

    g = LocalMemoryGraph(str(tmp_path / "graph.jsonl"))
    g.save_artifact_summary(
        job_id="j1", chain_id="cc", hop_id="h1", workflow_id="respond",
        artifact_name="OUT.md", kind="document", sha256="abc", size=3,
        summary="hello", task_redacted="t", verdict="SHIP")
    (tmp_path / "graph.jsonl").write_bytes(
        (tmp_path / "graph.jsonl").read_bytes() + b"\xff\n")
    assert len(g) >= 1  # must not raise


# ---------------------------------------------------------------- T8-F8
def test_no_stale_gemini_35_flash_claims():
    """Doc-truth: code + docs must say gemini-3.6-flash, not 3.5 Flash."""
    root = Path(__file__).resolve().parent.parent
    for rel in ["nine/runtime/gemma.py", "nine/chains/flagship.py",
                "nine/router/classifier.py", "deploy/server.py",
                "demo_live.py"]:
        txt = (root / rel).read_text()
        assert "3.5 Flash" not in txt, rel
    for rel in ["docs/architecture.svg", "docs/demo-script.md",
                "docs/ADAM-RUNBOOK.md"]:
        p = root / rel
        if p.exists():
            assert "3.5 Flash" not in p.read_text(), rel


# ---------------------------------------------------------------- T7-F1
def test_stale_eval_json_never_ships(tmp_path):
    """A FIX re-run that leaves a previous attempt's EVAL.json on disk must
    NOT SHIP on it: the certifying evidence must be produced this attempt."""
    calls = {"n": 0}

    def node_run(inputs, job_dir):
        calls["n"] += 1
        jd = Path(job_dir)
        if calls["n"] == 1:
            # attempt 1: EVAL.json passes but REQ.md missing -> FIX
            (jd / "EVAL.json").write_text(
                '{"checks":[{"name":"c","passed":true}]}', encoding="utf-8")
        else:
            # attempt 2: writes ONLY REQ.md - EVAL.json is stale
            (jd / "REQ.md").write_text("req", encoding="utf-8")
        return {"output": "ok"}

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    gate = EvidenceGate()
    gate.register_check("eval-json", eval_json_check())
    gate.register_check("artifacts", required_artifact_check(["REQ.md"]))
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("stale", {"task": "x"})
    wf = Workflow(id="stale")
    wf.add_node(Node(id="t1", kind="tool", run=node_run))
    res = ex.execute(wf, job, {"task": "x"})
    assert res["verdict"]["verdict"] == "BLOCK"
    assert "stale artifact" in res["verdict"]["summary"]
    assert "EVAL.json" in res["verdict"]["summary"]
    assert job.status == "blocked"


# ---------------------------------------------------------------- T7-F2
def test_flagship_hops_consume_fix_directive():
    """The flagship research/plan/build ADK instruction templates must
    include the fix_directive (blind retries were burning model budget)."""
    import nine.chains.flagship as fl

    assert fl._fix_directive_suffix("") == ""
    s = fl._fix_directive_suffix("gate FIX after attempt 1: missing PLAN.md")
    assert "gate FIX after attempt 1" in s
    for fn_name in ("_research_adk_node", "_plan_adk_node", "_build_adk_node"):
        import inspect as _i
        body = _i.getsource(getattr(fl, fn_name))
        assert "fix_dir = str(inputs.get(\"fix_directive\"" in body, fn_name
        assert "_fix_directive_suffix(fix_dir)" in body, fn_name


# ---------------------------------------------------------------- T7-F4
def test_explicit_chain_decision_is_honest(tmp_path):
    """CLI-style chain runs (decision=None) must stamp the CHAIN id, not a
    fabricated keyword route (respond/0.0) onto the job + LEARN events."""
    from nine.chains.chain import Chain, ChainExecutor, Hop
    from nine.learn.learner import RouteEventStore

    wf = Workflow(id="only")
    wf.add_node(Node(id="b1", kind="bash",
                     command="echo done > OUT.md; printf '{\"checks\":[{\"name\":\"c\",\"passed\":true}]}' > EVAL.json"))
    hop = Hop(id="only", workflow=wf,
              required_artifacts=["OUT.md", "EVAL.json"],
              gate_checks={"eval-json": eval_json_check()})
    chain = Chain(id="my-chain", hops=[hop])

    from nine.learn.learner import Learner

    events = RouteEventStore(tmp_path / "events.jsonl")
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    cex = ChainExecutor(ledger, workdir=tmp_path / "work",
                        learner=Learner(events))
    job = ledger.submit("my-chain", {"task": "customer wants a refund"})
    res = cex.execute(chain, job, {"task": "customer wants a refund"})
    assert res["final"] == "SHIPPED"
    rd = job.route_decision
    assert rd["workflow_id"] == "my-chain"
    assert rd["confidence"] == 1.0
    assert rd["model"] == "explicit-chain"
    assert rd["reason"] == "explicit chain invocation"
    evs = events.all()
    assert evs and all(e.workflow_id.startswith("my-chain::") for e in evs)


# ---------------------------------------------------------------- T7-F5
def test_recover_chain_fails_loud_one_line(tmp_path, monkeypatch):
    """recover of a chain job whose hop fails loud (no key) must print ONE
    [error] line, no traceback, exit 1 (mirror cmd_chain)."""
    from nine import cli

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    job = ledger.submit("research-plan-build-review-teach", {"task": "build a calculator"})
    job.status = "blocked"
    ledger.update(job)
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("build a calculator\n", encoding="utf-8")

    monkeypatch.setattr(cli, "_ledger", lambda args: ledger)
    monkeypatch.setattr(cli, "_learner", lambda args: None)
    args = type("A", (), {"job_id": job.job_id, "workdir": tmp_path / "work",
                          "force": False})()
    import contextlib
    import io
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        rc = cli.cmd_recover(args)
    assert rc == 1
    err_text = err.getvalue()
    assert "Traceback" not in err_text
    assert err_text.strip().count("\n") <= 2  # at most a warning + one error
    assert "[error]" in err_text or "error:" in err_text


# ---------------------------------------------------------------- T7-F6
def test_server_chunked_body_cap_413(tmp_path):
    """POST /v1/submit with a chunked (no content-length) body over 1 MiB
    must 413 instead of being fully buffered."""
    from fastapi.testclient import TestClient

    from deploy.server import MAX_BODY_BYTES, app

    client = TestClient(app)

    def gen():
        yield b'{"task": "'
        yield b"a" * (MAX_BODY_BYTES + 4096)
        yield b'"}'

    # httpx sends a generator as chunked transfer-encoding (no content-length)
    r = client.post("/v1/submit", content=gen())
    assert r.status_code == 413


# ---------------------------------------------------------------- T7-F8
def test_hop_artifacts_forwarded_to_node_inputs(tmp_path):
    """chain_inputs['hop_artifacts'] must reach node inputs (the documented
    artifact-passing contract was dead code)."""
    seen = {}

    def cap_run(inputs, job_dir):
        seen["hop_artifacts"] = inputs.get("hop_artifacts")
        (Path(job_dir) / "OUT.md").write_text("done", encoding="utf-8")
        (Path(job_dir) / "EVAL.json").write_text(
            '{"checks":[{"name":"c","passed":true}]}', encoding="utf-8")
        return {"output": "ok"}

    from nine.chains.chain import Chain, ChainExecutor, Hop

    h1 = Workflow(id="h1")
    h1.add_node(Node(id="b1", kind="bash",
                     command="echo A > A.md; printf '{\"checks\":[{\"name\":\"c\",\"passed\":true}]}' > EVAL.json"))
    hop1 = Hop(id="h1", workflow=h1, required_artifacts=["A.md", "EVAL.json"],
               gate_checks={"eval-json": eval_json_check()})
    h2 = Workflow(id="h2")
    h2.add_node(Node(id="t1", kind="tool", run=cap_run))
    hop2 = Hop(id="h2", workflow=h2, required_artifacts=["OUT.md", "EVAL.json"],
               gate_checks={"eval-json": eval_json_check()})

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    cex = ChainExecutor(ledger, workdir=tmp_path / "work")
    job = ledger.submit("hh", {"task": "x"})
    res = cex.execute(Chain(id="hh", hops=[hop1, hop2]), job, {"task": "x"})
    assert res["final"] == "SHIPPED"
    assert seen.get("hop_artifacts"), "hop_artifacts never reached the node"
    assert any("A.md" in str(v) for v in seen["hop_artifacts"].values())


# ---------------------------------------------------------------- T7-F3
def test_compose_refuses_builtin_id_collision(tmp_path):
    """A compose job whose WF_ID collides with a built-in workflow must BLOCK
    with a clear collision message and leave the built-in lane intact."""
    from nine.workflows.compose_wf import _compose_check

    wd = tmp_path
    (wd / "WF_ID.txt").write_text("build", encoding="utf-8")
    ok, msg = _compose_check({}, wd)
    assert not ok
    assert "BUILT-IN" in msg and "collision" in msg


def test_compose_refuses_stale_plugin_overwrite(tmp_path):
    """A plugin file that pre-exists and was NOT produced by this run must
    not be overwritten (gate BLOCKs)."""
    from nine.workflows import compose_wf
    from nine.workflows.compose_wf import _compose_check

    wd = tmp_path
    (wd / "WF_ID.txt").write_text("mytool", encoding="utf-8")
    existing = compose_wf._PLUGINS_DIR / "mytool_wf.py"
    try:
        existing.write_text("# stale leftover plugin\n" + "x" * 120)
        ok, msg = _compose_check({}, wd)
        assert not ok
        assert "not produced by this run" in msg
    finally:
        if existing.exists():
            existing.unlink()
