"""Slice 33 harvest tests — torture round 7 (torture-13 slices-30-32 audit +
torture-14 LEARN/memory/server/gate/fixtures).

15 findings, all hermetic (no network, no Gemini, stubs + real modules only).
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from nine.chains.chain import Chain, ChainExecutor, Hop
from nine.gates.evidence import (
    EvidenceGate,
    required_artifact_check,
)
from nine.ledger.ledger import JSONLLedger
from nine.runtime.workflows import Node, Workflow, WorkflowExecutor


def _flag_check(ctx, workdir) -> tuple[bool, str]:
    f = Path(workdir) / "FLAG.txt"
    ok = f.exists() and f.read_text().strip() == "ok"
    return ok, ("flag present" if ok else "FLAG.txt missing")


def _gate(checks: dict) -> EvidenceGate:
    g = EvidenceGate()
    for name, chk in checks.items():
        g.register_check(name, chk)
    return g


def _run_workflow(tmp_path, wf, checks, seed=None, fix_loops=2):
    gate = _gate(checks)
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit(wf.id, {"task": "t"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("t\n", encoding="utf-8")
    if seed:
        for name, content in seed.items():
            (job_dir / name).write_text(content, encoding="utf-8")
    res = ex.execute(wf, job, {"task": "t"}, fix_loop=fix_loops > 0)
    return res, job, job_dir


# ------------------------------------------------------------ torture-13 F1 ----
def test_t13_f1_recover_unknown_id_is_clean_one_line(tmp_path, capsys):
    """`nine recover <unknown-id>` must print ONE clean error line — the
    ledger.get path sat outside every try (slice-32 F8 missed it) and
    raw-tracebacked LedgerError."""
    import nine.cli as cli

    args = argparse.Namespace(ledger=str(tmp_path / "ledger.jsonl"),
                              job_id="deadbeef-dead-beef",
                              workdir=str(tmp_path / "work"), force=False)
    rc = cli.cmd_recover(args)
    out = capsys.readouterr()
    assert rc == 1
    assert "error:" in out.err
    assert "Traceback" not in out.err
    assert "Traceback" not in out.out


# ------------------------------------------------------------ torture-13 F2 ----
def test_t13_f2_runtime_records_bash_node_pid(tmp_path):
    """The runtime records each detached bash node's group-leader pid in
    .nine-node-pids (never a manifest entry) so an external killer (bench
    timeout) can clean up after the CLI dies."""
    wf = Workflow(id="pids")
    # long-running node: the pid line must EXIST while the node is alive
    # (an external killer's only window), then be PRUNED on normal
    # completion (T15-F9: stale pids must not be mis-attributed to a
    # recycled process later in the run).
    wf.add_node(Node(id="n", kind="bash",
                     command="echo hello > out.txt && sleep 3"))

    def _any(_ctx, workdir) -> tuple[bool, str]:
        return True, "always"
    gate = _gate({"any": _any})
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit(wf.id, {"task": "t"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("t\n", encoding="utf-8")
    pid_file = job_dir / ".nine-node-pids"

    res_holder: dict = {}
    def _run():
        res_holder["res"] = ex.execute(wf, job, {"task": "t"},
                                       fix_loop=True)
    th = threading.Thread(target=_run)
    th.start()
    # wait for the node to spin up and record its pid
    deadline = time.monotonic() + 15
    line = ""
    while time.monotonic() < deadline:
        if pid_file.exists() and pid_file.read_text().strip():
            line = pid_file.read_text().strip()
            break
        time.sleep(0.05)
    th.join(timeout=30)
    assert not th.is_alive(), "workflow did not finish in time"
    assert "res" in res_holder
    res = res_holder["res"]
    assert res["verdict"]["verdict"] == "SHIP"
    # T15-F9: each line is "pid spawn_epoch" (the runtime records the
    # spawn wall-clock so an external killer can verify pid identity
    # before SIGKILLing — a recycled pid must never be killed).
    assert line, "pid line never appeared while the node was running"
    pid, epoch = line.split()
    assert pid.isdigit() and int(pid) > 1
    assert epoch.isdigit() and int(epoch) > 1_000_000_000
    # T15-F9: the runtime prunes the line on NORMAL completion — no stale
    # pid survives the run to be mis-attributed to a recycled process.
    assert not pid_file.read_text().strip(),         "pid line should be pruned after normal node completion"
    names = [a["name"] for a in job.artifacts]
    assert ".nine-node-pids" not in names
    assert "out.txt" in names


def test_t13_f2_bench_kill_node_groups_kills_detached_groups(tmp_path):
    """bench_nine._kill_node_groups must SIGTERM/SIGKILL the runtime's
    DETACHED bash-node process groups (own session), which the CLI's
    killpg cannot reach after the CLI itself is dead."""
    from bench import bench_nine as bm

    proc = subprocess.Popen(["bash", "-c", "sleep 60"], start_new_session=True)
    job_dir = tmp_path / "work" / "j1"
    job_dir.mkdir(parents=True)
    (job_dir / ".nine-node-pids").write_text(f"{proc.pid}\n", encoding="utf-8")
    try:
        killed = bm._kill_node_groups(tmp_path / "work")
        assert killed >= 1
        # wait() reaps the child so poll() reflects the death (os.kill(pid,0)
        # would still "succeed" on a zombie)
        proc.wait(timeout=5)
        assert proc.poll() is not None
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


# ------------------------------------------------------------ torture-13 F3 ----
def test_t13_f3_run_modified_input_is_no_longer_exempt(tmp_path):
    """A gate-certified file seeded as a run input that the RUN rewrote in an
    earlier attempt is run-produced evidence: an attempt that produces
    nothing must BLOCK (the exemption is per-file-content, not per-name)."""

    def write_fn(inputs, job_dir):
        jd = Path(job_dir)
        if inputs.get("attempt") == 1:
            (jd / "required.txt").write_text("MODIFIED by run\n",
                                             encoding="utf-8")
        else:
            (jd / "FLAG.txt").write_text("ok\n", encoding="utf-8")
        return {"output": "done"}

    wf = Workflow(id="f3")
    wf.add_node(Node(id="n", kind="tool", run=write_fn))
    res, job, job_dir = _run_workflow(
        tmp_path, wf,
        {"flag": _flag_check, "artifacts": required_artifact_check(["required.txt"])},
        seed={"required.txt": "SEEDED content\n"})
    assert res["verdict"]["verdict"] == "BLOCK", res["verdict"]["summary"]
    assert "required.txt" in res["verdict"]["summary"]


def test_t13_f3_unchanged_run_input_still_exempt(tmp_path):
    """An UNCHANGED seeded input stays exempt — the normal task.txt /
    test_solution.py / HANDOFF.md flow must keep SHIPping."""

    def write_fn(inputs, job_dir):
        jd = Path(job_dir)
        if inputs.get("attempt") == 1:
            pass  # gate fails: no FLAG yet
        else:
            (jd / "FLAG.txt").write_text("ok\n", encoding="utf-8")
        return {"output": "done"}

    wf = Workflow(id="f3b")
    wf.add_node(Node(id="n", kind="tool", run=write_fn))
    res, job, job_dir = _run_workflow(
        tmp_path, wf,
        {"flag": _flag_check, "artifacts": required_artifact_check(["required.txt"])},
        seed={"required.txt": "SEEDED content\n"})
    assert res["verdict"]["verdict"] == "SHIP", res["verdict"]["summary"]


def test_t13_f3_chain_hop_rewriting_previous_handoff_then_idling_blocks(tmp_path):
    """Chain variant: hop2's attempt-1 snapshot contains hop1's HANDOFF.md
    (a legit run input for the hop). If hop2 REWRITES it in attempt 1 and
    then produces nothing in attempt 2, the gate certifies hop2's modified
    bytes while the manifest omits them -> must BLOCK (t12-F1 family closed
    through the exemption boundary)."""

    def hop1_run(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "HANDOFF.md").write_text("handoff v1\n", encoding="utf-8")
        # NOTE: no FLAG.txt — a leftover flag would satisfy h2's own flag
        # check on h2's FIRST attempt (the chain shares one job dir)
        return {"output": "hop1"}

    def hop2_run(inputs, job_dir):
        jd = Path(job_dir)
        if not inputs.get("fix_directive"):
            (jd / "HANDOFF.md").write_text("handoff REWRITTEN by hop2\n",
                                           encoding="utf-8")
        else:
            (jd / "FLAG.txt").write_text("ok\n", encoding="utf-8")
        return {"output": "hop2"}

    h1 = Workflow(id="h1")
    h1.add_node(Node(id="n", kind="tool", run=hop1_run))
    h2 = Workflow(id="h2")
    h2.add_node(Node(id="n", kind="tool", run=hop2_run))
    # h1's gate must NOT require FLAG.txt: h1 leaving one behind would
    # satisfy h2's flag check on h2's FIRST attempt (shared job dir).
    chain = Chain(id="c", hops=[
        Hop("h1", h1, ["HANDOFF.md"],
            {"artifacts": required_artifact_check(["HANDOFF.md"])}, 1),
        Hop("h2", h2, ["HANDOFF.md"],
            {"flag": _flag_check,
             "artifacts": required_artifact_check(["HANDOFF.md"])}, 1),
    ])
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = ChainExecutor(ledger, workdir=tmp_path / "work")
    job = ledger.submit("c", {"task": "t"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("t\n", encoding="utf-8")
    res = ex.execute(chain, job, {"task": "t"})
    assert res["final"] == "BLOCKED", res
    # the BLOCK must name the rewritten handoff (stale, missing from manifest)
    all_sums = " ".join(str(r) for r in res["hop_results"].values())
    assert "BLOCK" in all_sums


# ------------------------------------------------------------ torture-13 F4 ----
def test_t13_f4_subdir_artifact_path_registered_once(tmp_path):
    """Explicit artifact_path dedup must key on the RELATIVE name — the old
    basename key registered a subdir artifact TWICE."""

    def write_fn(inputs, job_dir):
        jd = Path(job_dir)
        docs = jd / "docs"
        docs.mkdir(exist_ok=True)
        (docs / "README.md").write_text("# doc\n", encoding="utf-8")
        return {"output": "done", "artifact_path": str(docs / "README.md")}

    wf = Workflow(id="f4")
    wf.add_node(Node(id="n", kind="tool", run=write_fn))

    def _any(_ctx, workdir):
        return True, "always"
    res, job, job_dir = _run_workflow(tmp_path, wf, {"any": _any})
    names = [a["name"] for a in job.artifacts]
    assert names.count("docs/README.md") == 1, names


# ------------------------------------------------------------ torture-13 F5 ----
def test_t13_f5_bench_archives_default_runid_r0(tmp_path, monkeypatch):
    """The documented default invocation (RUNID=r0) must archive its
    scorecard too — the old code skipped r0, silently destroying the
    previous run's results with no comparison source."""
    from bench import bench_nine as bm

    fx = tmp_path / "fx"
    fx.mkdir(parents=True)
    (fx / "task.md").write_text("# Eval Fixture: fx\n## Task Description\nmake it\n",
                                encoding="utf-8")
    (fx / "tests").mkdir(parents=True)
    (fx / "tests" / "check.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (fx / "starter").mkdir()
    (fx / "starter" / "solution.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(bm, "FIXTURES", ["fx"])
    monkeypatch.setattr(bm, "FIXTURES_DIR", tmp_path)
    monkeypatch.setattr(bm, "BENCH_ROOT", tmp_path / "runs")
    monkeypatch.setattr(bm, "RUNID", "r0")

    def fake_ok(fixture_dir, workdir, ledger):
        return ({"job_id": "j1", "workflow_id": "debug", "router_model": "m",
                 "verdict": "SHIP", "verdict_summary": "ok", "final_status": "shipped",
                 "attempts": 1, "timed_out": False, "cli_error": None}, "", "")

    monkeypatch.setattr(bm, "run_submit", fake_ok)
    rc = bm.main()
    assert rc == 0
    assert (tmp_path / "runs" / "results-r0.json").exists()


# ------------------------------------------------------------ torture-14 F1 ----
def test_t14_f1_catalog_cannot_reroute_to_demo_chain(tmp_path, monkeypatch, capsys):
    """The canned demo lane must stay unroutable even through LEARN catalog
    keyword overrides (T5-F2's keyword ban was only in _BASE_KEYWORDS)."""
    import nine.registry as reg
    from nine.router.classifier import Router

    monkeypatch.setattr(reg, "load_catalog", lambda: {
        "keyword_overrides": {"inbox-triage-task-report": ["refund"]}})
    merged = reg._merged_keywords()
    err = capsys.readouterr().err
    assert "inbox-triage-task-report" not in merged
    assert "non-routable" in err
    r = Router()
    for wf_id, kws in merged.items():
        r.register(wf_id, kws, "")
    d = r.classify("customer wants a refund on their order")
    assert d.workflow_id != "inbox-triage-task-report"


def test_t14_f1_learn_apply_refuses_non_routable(tmp_path, monkeypatch, capsys):
    import nine.cli as cli
    import nine.registry as reg
    from nine.learn.learner import CandidateStore, ImprovementCandidate

    store = CandidateStore(tmp_path / "cands.jsonl")
    store.append(ImprovementCandidate(
        candidate_id="c1", kind="keyword", description="d", evidence=[],
        params={"workflow_id": "inbox-triage-task-report", "keyword": "refund"}))
    learner = type("L", (), {"cands": store})()
    saved = {}

    def fake_load():
        return {"keyword_overrides": {}}

    def fake_save(cat):
        saved["cat"] = cat

    monkeypatch.setattr(reg, "load_catalog", fake_load)
    monkeypatch.setattr(reg, "save_catalog", fake_save)
    rc = cli._apply_candidate(learner, "c1")
    assert rc == 1
    assert "non-routable" in capsys.readouterr().err
    assert "cat" not in saved  # catalog untouched


# ------------------------------------------------------------ torture-14 F2 ----
def test_t14_f2_memory_summaries_are_redacted_and_per_artifact(tmp_path):
    """The memory write-path must distill+redact: no raw credentials from
    HANDOFF.md may land in memory records, and later hops' summaries must
    quote their OWN artifact, not the plan handoff."""
    from nine.memory.graph import LocalMemoryGraph

    def hop1_run(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "HANDOFF.md").write_text(
            "handoff\naws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
            "\nAPI_KEY=sk-ABCDEFGHIJKLMNOP123456\n", encoding="utf-8")
        (jd / "FLAG.txt").write_text("ok\n", encoding="utf-8")
        return {"output": "hop1"}

    def hop2_run(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "solution.py").write_text("print('hop2 content')\n",
                                        encoding="utf-8")
        (jd / "FLAG.txt").write_text("ok\n", encoding="utf-8")
        return {"output": "hop2"}

    h1 = Workflow(id="h1")
    h1.add_node(Node(id="n", kind="tool", run=hop1_run))
    h2 = Workflow(id="h2")
    h2.add_node(Node(id="n", kind="tool", run=hop2_run))
    chain = Chain(id="c", hops=[
        Hop("h1", h1, ["HANDOFF.md"], {"flag": _flag_check,
             "artifacts": required_artifact_check(["HANDOFF.md"])}, 1),
        Hop("h2", h2, ["solution.py"], {"flag": _flag_check,
             "artifacts": required_artifact_check(["solution.py"])}, 1),
    ])
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    mem = LocalMemoryGraph(tmp_path / "memory.jsonl")
    ex = ChainExecutor(ledger, workdir=tmp_path / "work", memory=mem)
    job = ledger.submit("c", {"task": "t"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("t\n", encoding="utf-8")
    res = ex.execute(chain, job, {"task": "t"})
    assert res["final"] == "SHIPPED", res

    recs = [json.loads(line) for line in
            (tmp_path / "memory.jsonl").read_text().splitlines() if line.strip()]
    assert len(recs) >= 2
    blob = json.dumps(recs)
    assert "wJalrXUtnFEMI" not in blob
    assert "sk-ABCDEFGHIJKLMNOP123456" not in blob
    sol_recs = [r for r in recs
                if r["hop_id"] == "h2" and r["artifact_name"] == "solution.py"]
    assert sol_recs, [r["artifact_name"] for r in recs if r["hop_id"] == "h2"]
    # the build hop's own artifact is summarized from ITS content, not the
    # plan handoff
    assert "hop2 content" in sol_recs[0]["summary"], sol_recs[0]["summary"]


# ------------------------------------------------------------ torture-14 F3 ----
def test_t14_f3_flagship_self_test_failure_branch_counts_passed(tmp_path):
    """The flagship build self-test failure branch must parse the pytest -q
    summary (grep -c ' PASSED' always returned 0)."""
    from nine.chains.flagship import _build_self_test_command

    ts = tmp_path / "test_solution.py"
    ts.write_text(
        "def test_a():\n    assert 1 == 1\n"
        "def test_b():\n    assert 2 == 2\n"
        "def test_c():\n    assert 1 == 2\n"
        "def test_d():\n    assert 1 == 3\n"
        "def test_e():\n    assert 1 == 4\n", encoding="utf-8")
    subprocess.run(["bash", "-c", _build_self_test_command()],
                   cwd=tmp_path, capture_output=True, text=True, check=False)
    ev = json.loads((tmp_path / "EVAL.json").read_text())
    msg = ev["checks"][0]["message"]
    assert ev["checks"][0]["passed"] is False
    assert "3 test(s) failed, 2 passed" in msg, msg


def test_t14_f3_test_lane_runner_failure_branch_counts_passed(tmp_path):
    from nine.workflows.test_wf import _build_test_runner_command

    ts = tmp_path / "test_solution.py"
    ts.write_text(
        "def test_a():\n    assert 1 == 1\n"
        "def test_b():\n    assert 2 == 2\n"
        "def test_c():\n    assert 1 == 2\n", encoding="utf-8")
    subprocess.run(["bash", "-c", _build_test_runner_command()],
                   cwd=tmp_path, capture_output=True, text=True, check=False)
    ev = json.loads((tmp_path / "EVAL.json").read_text())
    msg = ev["checks"][0]["message"]
    assert ev["checks"][0]["passed"] is False
    assert "1 test(s) failed, 2 passed" in msg, msg


# ------------------------------------------------------------ torture-14 F4 ----
def test_t14_f4_manifest_ignores_cache_and_dedupes_same_name(tmp_path):
    """Two nodes writing the same file within one attempt (build + self-test
    both write EVAL.json) must leave ONE entry (last writer = disk state the
    gate certifies); pytest cache/pyc/log byproducts are never evidence."""

    def n1(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "EVAL.json").write_text('{"v": 1}', encoding="utf-8")
        return {"output": "a"}

    def n2(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "EVAL.json").write_text('{"v": 2}', encoding="utf-8")
        (jd / "test_output.log").write_text("log\n", encoding="utf-8")
        (jd / "x.cpython-314.pyc").write_bytes(b"pyc")
        pc = jd / ".pytest_cache" / "v"
        pc.mkdir(parents=True)
        (pc / "nodeids").write_text("x\n", encoding="utf-8")
        return {"output": "b"}

    import hashlib
    wf = Workflow(id="f4b")
    wf.add_node(Node(id="n1", kind="tool", run=n1))
    wf.add_node(Node(id="n2", kind="tool", run=n2, depends_on=["n1"]))
    res, job, job_dir = _run_workflow(
        tmp_path, wf,
        {"artifacts": required_artifact_check(["EVAL.json"])})
    assert res["verdict"]["verdict"] == "SHIP", res["verdict"]["summary"]
    names = [a["name"] for a in job.artifacts]
    assert names.count("EVAL.json") == 1, names
    ev = next(a for a in job.artifacts if a["name"] == "EVAL.json")
    assert ev["sha256"] == hashlib.sha256(b'{"v": 2}').hexdigest(), names
    assert "test_output.log" not in names
    assert not any(n.endswith(".pyc") for n in names)
    assert not any(".pytest_cache" in n for n in names)


# ------------------------------------------------------------ torture-14 F5 ----
def test_t14_f5_convert_inlines_runner_constants(tmp_path):
    from bench.bench_nine import convert_to_pytest

    runner = (
        "from implementation import add\n"
        "EXPECTED_SUM = 5\n"
        'test("constant", lambda: add(2, 3), EXPECTED_SUM)\n'
    )
    src = convert_to_pytest(runner)
    assert "== 5" in src
    # the converted suite must RUN without NameError
    (tmp_path / "solution.py").write_text(
        "def add(a, b):\n    return a + b\n", encoding="utf-8")
    (tmp_path / "test_converted.py").write_text(src, encoding="utf-8")
    r = subprocess.run([sys.executable, "-m", "pytest", "test_converted.py",
                        "-q"], cwd=tmp_path, capture_output=True, text=True,
                       check=False)
    assert r.returncode == 0, r.stdout + r.stderr


def test_t14_f5_convert_refuses_dangling_helper(tmp_path):
    from bench.bench_nine import convert_to_pytest

    runner = (
        "from implementation import add\n"
        "def twice(x):\n    return x * 2\n"
        'test("helper", lambda: twice(3), 6)\n'
    )
    with pytest.raises(RuntimeError, match="twice"):
        convert_to_pytest(runner)


# ------------------------------------------------------------ torture-14 F6 ----
def test_t14_f6_server_chain_submit_surfaces_hop_verdicts(tmp_path, monkeypatch):
    """POST /v1/submit on a chain route must return a real verdict object
    (final + per-hop evidence) — not 'verdict: {}' on a shipped chain."""
    import deploy.server as server
    from nine.router.classifier import RouteDecision

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    monkeypatch.setattr(server, "get_ledger", type("G", (), {"__call__": lambda self: ledger})())
    monkeypatch.setattr(server, "WORKDIR", tmp_path / "work")
    monkeypatch.setattr(server, "EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr(server, "MEMORY_PATH", tmp_path / "memory.jsonl")

    class _R:
        def classify(self, task):
            return RouteDecision(
                workflow_id="research-plan-build-review-teach",
                task_redacted="t",
                confidence=1.0,
                router_version="test",
                model="test",
                decision_id="d-1",
                reason="test",
                decided_at="2026-01-01T00:00:00+00:00",
            )

    monkeypatch.setattr(server, "build_router", type("BR", (), {"__call__": lambda self: _R()})())

    def fake_execute(self, chain, job, inputs, decision=None):
        return {"final": "SHIPPED",
                "hop_results": {"plan:1": {"verdict": "SHIP", "job_id": "x",
                                            "eval": {}}}}

    from nine.chains import chain as chain_mod

    monkeypatch.setattr(chain_mod.ChainExecutor, "execute", fake_execute)
    body = server.submit(server.SubmitRequest(task="t"))
    assert body["final"] == "SHIPPED"
    assert body["verdict"]["verdict"] == "SHIPPED"
    assert "plan:1" in body["verdict"]["hops"]
    assert body["verdict"]["hops"]["plan:1"]["verdict"] == "SHIP"


# ------------------------------------------------------------ torture-14 F7 ----
def test_t14_f7_learn_memory_store_construction_is_clean(tmp_path, capsys):
    """A bad --events/--memory path must print ONE clean line (T12-F8 was
    ledger-only; learn + memory stores raw-tracebacked FileExistsError)."""
    import nine.cli as cli

    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file\n", encoding="utf-8")
    bad = str(blocker / "sub" / "events.jsonl")

    rc = cli.cmd_learn(argparse.Namespace(events=bad, action="events"))
    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err
    assert "Traceback" not in err

    rc = cli.cmd_memory(argparse.Namespace(memory=bad, action="list"))
    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err
    assert "Traceback" not in err


def test_t14_f7_chain_bad_memory_path_is_clean(tmp_path, capsys):
    import nine.cli as cli

    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file\n", encoding="utf-8")
    bad = str(blocker / "sub" / "memory.jsonl")
    rc = cli.cmd_chain(argparse.Namespace(
        chain_id="demo", task="t", ledger=str(tmp_path / "ledger.jsonl"),
        workdir=str(tmp_path / "work"), events=str(tmp_path / "e.jsonl"),
        memory=bad))
    err = capsys.readouterr().err
    assert rc == 1
    assert "error:" in err
    assert "Traceback" not in err


# ------------------------------------------------------------ torture-14 F8 ----
def test_t14_f8_learn_apply_revert_wrong_shape_bucket(tmp_path, monkeypatch, capsys):
    """A valid-JSON string keyword_overrides bucket must refuse apply/revert
    with the shape warning — never AttributeError."""
    import nine.cli as cli
    import nine.registry as reg
    from nine.learn.learner import CandidateStore, ImprovementCandidate

    monkeypatch.setattr(reg, "load_catalog", lambda: {
        "keyword_overrides": {"research": "oops-not-a-list"}})
    saved = {}
    monkeypatch.setattr(reg, "save_catalog", lambda cat: saved.update(cat=cat))
    monkeypatch.setattr(cli, "_regression_green", lambda: True)

    def _learner_with(status, store_path):
        store = CandidateStore(store_path)
        store.append(ImprovementCandidate(
            candidate_id="c1", kind="keyword", description="d", evidence=[],
            status=status,
            params={"workflow_id": "research", "keyword": "zzztoken"}))
        return type("L", (), {"cands": store})()

    rc = cli._apply_candidate(_learner_with("pending", tmp_path / "a.jsonl"), "c1")
    assert rc == 1
    assert "not a list" in capsys.readouterr().err
    assert "cat" not in saved  # never mutated

    rc = cli._revert_candidate(_learner_with("applied", tmp_path / "r.jsonl"), "c1")
    assert rc == 1
    assert "not a list" in capsys.readouterr().err
    assert "cat" not in saved


# ------------------------------------------------------------ torture-14 F9 ----
def test_t14_f9_memory_list_and_search_skip_wrong_shape_records(tmp_path, capsys):
    """A valid-JSON wrong-shape memory line must not KeyError-crash list or
    search (ledger got _looks_like_job; memory records never did)."""
    import nine.cli as cli

    mem_path = tmp_path / "memory.jsonl"
    mem_path.write_text(
        '{"memory_id": "mem-ok-1", "chain_id": "c", "hop_id": "h", '
        '"artifact_name": "a.md", "verdict": "SHIP", "created_at": "2026-01-01", '
        '"summary": "hello world"}\n'
        '{"memory_id": "mem-x", "unexpected": 1}\n'
        '{"summary": "hello secret world", "artifact_name": "a"}\n',
        encoding="utf-8")
    rc = cli.cmd_memory(argparse.Namespace(memory=str(mem_path), action="list"))
    out = capsys.readouterr()
    assert rc == 0
    assert "Traceback" not in out.err
    assert "mem-ok-1" in out.out

    rc = cli.cmd_memory(argparse.Namespace(memory=str(mem_path),
                                           action="search", query="hello"))
    out = capsys.readouterr()
    assert rc == 0
    assert "Traceback" not in out.err


# ------------------------------------------------------------ torture-14 F10 ----
def test_t14_f10_server_bad_data_dir_returns_clean_502(tmp_path, monkeypatch):
    """An unusable NINE_DATA_DIR must produce a clean JSON 502 with the
    reason on every ledger endpoint — not raw 500s + a per-request
    Firestore retry storm."""
    from fastapi.testclient import TestClient

    import deploy.server as server

    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file\n", encoding="utf-8")
    monkeypatch.setattr(server, "LEDGER_PATH", blocker / "ledger.jsonl")
    monkeypatch.setattr(server, "EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr(server, "MEMORY_PATH", tmp_path / "memory.jsonl")
    monkeypatch.setattr(server, "_ledger", None)
    monkeypatch.setattr(server, "_ledger_failed", False)
    monkeypatch.setattr(server, "get_learner", lambda: None)

    client = TestClient(server.app)
    r = client.get("/v1/jobs")
    assert r.status_code == 502, r.text
    assert "detail" in r.json()
    assert "File exists" in r.json()["detail"] or "No such file" in r.json()["detail"]         or "Not a directory" in r.json()["detail"]
    r2 = client.get("/v1/stats")
    assert r2.status_code == 502, r2.text
