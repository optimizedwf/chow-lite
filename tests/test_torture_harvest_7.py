"""Slice 32 harvest tests — torture round 6 (torture-11 bench/gate + torture-12 chains/ledger/registry/CLI).

16 findings + 1 bonus, all hermetic (no network, no Gemini, stubs only).
"""
import json
import subprocess
import threading
import time
from pathlib import Path

import pytest

from nine.chains.chain import Chain, ChainExecutor, Hop
from nine.gates.evidence import (
    EvidenceGate,
    required_artifact_check,
)
from nine.ledger.ledger import Job, JSONLLedger, LedgerError
from nine.runtime.workflows import Node, Workflow, WorkflowExecutor


# ------------------------------------------------------------ torture-12 F1 ----
def _flag_check(ctx, workdir) -> tuple[bool, str]:
    f = Path(workdir) / "FLAG.txt"
    ok = f.exists() and f.read_text().strip() == "ok"
    return ok, ("flag present" if ok else "FLAG.txt missing")


def _make_chain_hop(write_fn, hop_id="h1", max_fix_loops=1,
                    required=("research.md", "HANDOFF.md")):
    wf = Workflow(id=hop_id)
    wf.add_node(Node(id="n", kind="tool", run=write_fn,
                     description="hermetic hop node"))
    gate_checks = {
        "flag": _flag_check,
        "artifacts": required_artifact_check(list(required)),
    }
    return Chain(id="c", hops=[Hop(hop_id, wf, list(required), gate_checks, max_fix_loops)])


def test_t12_f1_chain_stale_guard_blocks_retry_that_produces_nothing(tmp_path):
    """Chain hop FIX retries must share the attempt-1 run-input snapshot:
    a retry that writes NO artifact must BLOCK on stale evidence, exactly
    like the single-workflow path (the executor was fresh per attempt, so
    attempt-1 files became 'run inputs' and SHIPped)."""
    def write_fn(inputs, job_dir):
        jd = Path(job_dir)
        if not inputs.get("fix_directive"):
            # first attempt: produce the artifacts but fail the flag check
            (jd / "research.md").write_text("# research\n", encoding="utf-8")
            (jd / "HANDOFF.md").write_text("handoff\n", encoding="utf-8")
        else:
            # chain retry: the node produces NOTHING new (only a flag)
            (jd / "FLAG.txt").write_text("ok\n", encoding="utf-8")
        return {"output": "done"}

    chain = _make_chain_hop(write_fn)
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = ChainExecutor(ledger, workdir=tmp_path / "work")
    job = ledger.submit("c", {"task": "t"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("t\n", encoding="utf-8")
    res = ex.execute(chain, job, {"task": "t"})
    assert res["final"] == "BLOCKED", res
    # the last hop attempt's verdict must be BLOCK with the stale summary
    hop_jobs = [j for j in ledger.discover() if j.workflow_id == "c::h1"]
    assert hop_jobs
    # discover() is newest-first; the LAST attempt's job is the newest
    last = hop_jobs[0]
    assert last.verdicts[-1]["verdict"] == "BLOCK"
    assert "stale artifact" in last.verdicts[-1]["summary"]
    assert "research.md" in last.verdicts[-1]["summary"]


def test_t12_f1_single_workflow_stale_guard_still_blocks(tmp_path):
    """Same scenario through the single-workflow executor: the guard keeps
    the pre-fix behavior (BLOCK), proving chain + single paths now agree."""

    def write_fn(inputs, job_dir):
        jd = Path(job_dir)
        if inputs.get("attempt") == 1:
            (jd / "research.md").write_text("# research\n", encoding="utf-8")
            (jd / "HANDOFF.md").write_text("handoff\n", encoding="utf-8")
        else:
            (jd / "FLAG.txt").write_text("ok\n", encoding="utf-8")
        return {"output": "done"}

    wf = Workflow(id="h1")
    wf.add_node(Node(id="n", kind="tool", run=write_fn))
    gate = EvidenceGate()
    gate.register_check("flag", _flag_check)
    gate.register_check("artifacts", required_artifact_check(["research.md", "HANDOFF.md"]))
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("h1", {"task": "t"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("t\n", encoding="utf-8")
    res = ex.execute(wf, job, {"task": "t"})
    assert res["verdict"]["verdict"] == "BLOCK"
    assert "stale artifact" in res["verdict"]["summary"]


def test_t12_f1_chain_retry_that_rewrites_ships(tmp_path):
    """A retry that DOES rewrite the artifacts must still SHIP (no false
    positives from the persisted snapshot)."""

    def write_fn(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "research.md").write_text("# research v{}\n".format(inputs.get("attempt")), encoding="utf-8")
        (jd / "HANDOFF.md").write_text("handoff v{}\n".format(inputs.get("attempt")), encoding="utf-8")
        (jd / "FLAG.txt").write_text("ok\n", encoding="utf-8")
        return {"output": "done"}

    chain = _make_chain_hop(write_fn)
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = ChainExecutor(ledger, workdir=tmp_path / "work")
    job = ledger.submit("c", {"task": "t"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("t\n", encoding="utf-8")
    res = ex.execute(chain, job, {"task": "t"})
    assert res["final"] == "SHIPPED", res


# ------------------------------------------------------------ torture-12 F2 ----
def test_t12_f2_fix_directive_does_not_bleed_into_later_hops(tmp_path):
    """After hop1 FIXes and SHIPs, hop2 must NOT receive hop1's stale
    fix_directive in its model inputs (flagship ADK nodes append it to the
    instruction as 'Previous attempt failed the gate: ...')."""

    def hop1_run(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "OUT1.txt").write_text("out\n", encoding="utf-8")
        if inputs.get("fix_directive"):
            # chain retry: fix the flag check so this attempt SHIPs
            (jd / "FLAG.txt").write_text("ok\n", encoding="utf-8")
        return {"output": "hop1"}

    def hop2_run(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "capture.json").write_text(json.dumps({
            "fix_directive": inputs.get("fix_directive", ""),
            "attempt": inputs.get("attempt"),
        }), encoding="utf-8")
        return {"output": "hop2"}

    h1_wf = Workflow(id="h1")
    h1_wf.add_node(Node(id="n", kind="tool", run=hop1_run))
    g1 = {"flag": _flag_check,
          "artifacts": required_artifact_check(["OUT1.txt"])}
    h2_wf = Workflow(id="h2")
    h2_wf.add_node(Node(id="n", kind="tool", run=hop2_run))
    g2 = {"artifacts": required_artifact_check(["capture.json"])}

    chain = Chain(id="c", hops=[
        Hop("h1", h1_wf, ["OUT1.txt"], g1, 1),
        Hop("h2", h2_wf, ["capture.json"], g2, 0),
    ])
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = ChainExecutor(ledger, workdir=tmp_path / "work")
    job = ledger.submit("c", {"task": "t"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("t\n", encoding="utf-8")
    res = ex.execute(chain, job, {"task": "t"})
    assert res["final"] == "SHIPPED", res
    cap = json.loads((job_dir / "capture.json").read_text(encoding="utf-8"))
    assert cap["attempt"] == 1
    assert cap["fix_directive"] == "", cap["fix_directive"]


# ------------------------------------------------------------ torture-12 F3 ----
def test_t12_f3_recover_refuses_when_durable_state_is_shipped(tmp_path):
    """recover() must check the DURABLE file, not the construction-time
    cache: a stale cache saying 'blocked' must NOT re-recover a job that
    another process already shipped."""
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    job = ledger.submit("research", {"task": "x"})
    # mutate the CACHED object to 'blocked' (stale view)
    job.status = "blocked"
    # another process ships the job: append a shipped terminal line
    j2 = Job(workflow_id="research", job_id=job.job_id, input={"task": "x"})
    j2.status = "shipped"
    ledger.update(j2)
    with pytest.raises(LedgerError, match="shipped"):
        ledger.recover(job.job_id)
    # durable last line is still the shipped one
    fresh = ledger.refresh(job.job_id)
    assert fresh.status == "shipped"


def test_t12_f3_cancel_refuses_when_durable_state_is_shipped(tmp_path):
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    job = ledger.submit("research", {"task": "x"})
    job.status = "running"  # stale cache view
    j2 = Job(workflow_id="research", job_id=job.job_id, input={"task": "x"})
    j2.status = "shipped"
    ledger.update(j2)
    with pytest.raises(LedgerError):
        ledger.cancel(job.job_id)
    assert ledger.refresh(job.job_id).status == "shipped"


# ------------------------------------------------------------ torture-12 F4 ----
def test_t12_f4_review_hop_keeps_build_eval_json(tmp_path, monkeypatch):
    """The flagship review hop must write review-eval.json, never clobber the
    build hop's EVAL.json (before: two conflicting EVAL.json entries in the
    manifest + the review-consistent check compared review.md against the
    review's OWN just-written EVAL — circular)."""
    from nine.chains import flagship

    def fake_build_run(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "solution.py").write_text("def answer():\n    return 42\n", encoding="utf-8")
        (jd / "test_solution.py").write_text(
            "from solution import answer\ndef test_answer():\n    assert answer() == 42\n",
            encoding="utf-8")
        return {"output": "wrote solution.py"}

    monkeypatch.setattr(
        flagship, "_build_adk_node",
        lambda: Node(id="build", kind="tool", run=fake_build_run,
                     description="fake build node (hermetic)"))
    chain = Chain(id="t", hops=[flagship.build_hop(), flagship.review_hop()])
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = ChainExecutor(ledger, workdir=tmp_path / "work")
    job = ledger.submit("t", {"task": "build a tiny thing"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("build a tiny thing\n", encoding="utf-8")
    res = ex.execute(chain, job, {"task": "build a tiny thing"})
    assert res["final"] == "SHIPPED", res
    # build's EVAL.json survives (self-test content, not review's)
    build_ev = json.loads((job_dir / "EVAL.json").read_text(encoding="utf-8"))
    names = [c["name"] for c in build_ev.get("checks", [])]
    assert "tests-pass" in names or "multi-build-verified" in names
    assert "review-pass" not in names
    # review's OWN eval artifact is distinct
    rev_ev = json.loads((job_dir / "review-eval.json").read_text(encoding="utf-8"))
    assert "review-pass" in [c["name"] for c in rev_ev.get("checks", [])]
    # manifest: one EVAL.json (build) + review-eval.json, no duplicates
    names_all = [a["name"] for a in ledger.get(job.job_id).artifacts]
    assert names_all.count("EVAL.json") == 1
    assert "review-eval.json" in names_all


# ------------------------------------------------------------ torture-12 F5 ----
def test_t12_f5_recover_refuses_chain_hop_job_cleanly(tmp_path, capsys):
    """`nine recover <chain-hop-job-id>` must refuse with ONE clean line and
    NOT tombstone the job (before: wiped the dir + transitioned to
    recovered, then raw-tracebacked 'unregistered workflow id')."""
    from types import SimpleNamespace

    from nine.cli import cmd_recover

    wd = tmp_path / "work"
    wd.mkdir()
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    job = ledger.submit("research-plan-build-review-teach::build", {"task": "x"})
    job.status = "blocked"
    ledger.update(job)
    jd = wd / job.job_id
    jd.mkdir(parents=True, exist_ok=True)
    (jd / "task.txt").write_text("x\n", encoding="utf-8")
    args = SimpleNamespace(job_id=job.job_id, force=False, workdir=str(wd),
                           ledger=str(tmp_path / "ledger.jsonl"),
                           events=str(tmp_path / "events.jsonl"))
    rc = cmd_recover(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "not registered" in err
    assert "Traceback" not in err
    # no tombstone: job dir intact, status still blocked
    assert (jd / "task.txt").exists()
    assert ledger.refresh(job.job_id).status == "blocked"


# ------------------------------------------------------------ torture-12 F6 ----
def test_t12_f6_keywords_for_dead_ids_dropped(tmp_path, monkeypatch, capsys):
    from nine import registry as reg

    monkeypatch.setattr(reg, "load_catalog", lambda: {
        "keyword_overrides": {
            "ghost-id": ["do ghost work"],
            "research": ["extra research words"],
        }})
    merged = reg._merged_keywords()
    assert "ghost-id" not in merged
    assert "research" in merged
    assert "dropped" in capsys.readouterr().err


def test_t12_f6_cmd_submit_refuses_dead_routed_id(tmp_path, monkeypatch, capsys):
    from types import SimpleNamespace

    from nine import cli as cli_mod
    from nine.router.classifier import RouteDecision

    fake = RouteDecision(decision_id="d1", task_redacted="do ghost work",
                         workflow_id="ghost-id", confidence=0.9,
                         reason="learned keyword for removed plugin",
                         decided_at="2026-08-14T00:00:00+00:00",
                         router_version="test", model="test")
    monkeypatch.setattr(cli_mod, "build_default_router",
                        type("R", (), {"classify": lambda self, t: fake}))
    ledger_path = tmp_path / "ledger.jsonl"
    args = SimpleNamespace(task="do ghost work", workdir=str(tmp_path / "work"),
                           ledger=str(ledger_path), events=str(tmp_path / "e.jsonl"),
                           chain=False, plugin=None, model="")
    rc = cli_mod.cmd_submit(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "not registered" in err
    assert "Traceback" not in err
    # no job was submitted
    assert not ledger_path.exists()


# ------------------------------------------------------------ torture-12 F7 ----
def test_t12_f7_broken_plugin_registry_warns_loudly(tmp_path, monkeypatch, capsys):
    from nine import registry as reg

    bad = tmp_path / "plugin_registry.py"
    bad.write_text("this is not valid python !!!\n", encoding="utf-8")
    monkeypatch.setenv("NINE_PLUGIN_REGISTRY", str(bad))
    out = reg._load_plugin_workflows()
    assert out == {}
    assert "failed to load" in capsys.readouterr().err


# ------------------------------------------------------------ torture-12 F8 ----
def test_t12_f8_ledger_construction_oserror_is_ledger_error(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file\n", encoding="utf-8")
    with pytest.raises(LedgerError):
        JSONLLedger(blocker / "ledger.jsonl")


def test_t12_f8_cli_commands_catch_ledger_error(tmp_path, capsys):
    from types import SimpleNamespace

    from nine import cli as cli_mod

    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file\n", encoding="utf-8")
    bad = str(blocker / "ledger.jsonl")
    for fn in (cli_mod.cmd_status, cli_mod.cmd_discover, cli_mod.cmd_stats):
        args = SimpleNamespace(job_id="x", ledger=bad, status=None,
                               workdir=str(tmp_path / "w"))
        rc = fn(args)
        assert rc == 1
        assert capsys.readouterr().err.startswith("error:")


# ------------------------------------------------------------ torture-11 F1 ----
def test_t11_f1_seed_worker_seeds_newest_job_dir(tmp_path):
    """A pre-existing job dir (repeat RUNID) must not suppress seeding for
    the newly created dir; the seeder returns only once the NEWEST dir is
    seeded."""
    from bench.bench_nine import seed_worker

    workdir = tmp_path / "work"
    workdir.mkdir()
    old = workdir / "old-job"
    old.mkdir()
    (old / "solution.py").write_text("old", encoding="utf-8")
    (old / "test_solution.py").write_text("old", encoding="utf-8")
    sol = tmp_path / "sol.py"
    sol.write_text("print(1)\n", encoding="utf-8")
    tst = tmp_path / "tst.py"
    tst.write_text("def test_x():\n    pass\n", encoding="utf-8")
    stop = threading.Event()

    def spawn():
        time.sleep(0.05)
        (workdir / "new-job").mkdir()

    th = threading.Thread(target=spawn)
    th.start()
    seed_worker(workdir, sol, tst, stop)
    th.join(timeout=2)
    assert (workdir / "new-job" / "solution.py").read_text() == "print(1)\n"
    assert (workdir / "new-job" / "test_solution.py").read_text() == "def test_x():\n    pass\n"
    # the old dir was re-seeded too (idempotent overwrite)
    assert (old / "solution.py").read_text() == "print(1)\n"


# ------------------------------------------------------------ torture-11 F2/F3 --
def _run_verify(tmp_path, test_src):
    from nine.workflows.debug_wf import _build_verify_command

    (tmp_path / "test_solution.py").write_text(test_src, encoding="utf-8")
    (tmp_path / "solution.py").write_text("x = 1\n", encoding="utf-8")
    cmd = _build_verify_command()
    r = subprocess.run(["bash", "-c", cmd], cwd=tmp_path,
                       capture_output=True, text=True, timeout=60, check=False)
    assert r.returncode == 0
    ev = json.loads((tmp_path / "EVAL.json").read_text(encoding="utf-8"))
    return ev


def test_t11_f2_failing_test_with_valueerror_is_not_collection_error(tmp_path):
    """A plain test failure whose output mentions 'valueerror' must be
    reported as FAILED TESTS, not 'pytest collection error' (fixtures
    002/003 slugs are raises_valueerror — this was live in slice-31's 002
    fix loop)."""
    ev = _run_verify(tmp_path, (
        "def test_raises_valueerror():\n"
        "    assert 1 == 2\n"
    ))
    msg = ev["checks"][0]["message"]
    assert ev["checks"][0]["passed"] is False
    assert "collection error" not in msg
    assert "failed" in msg


def test_t11_f3_pytest_q_summary_counts_are_parsed(tmp_path):
    """pytest -q never prints ' PASSED' per test — the verify node must
    parse the summary line ('N failed, M passed'), not grep for PASSED."""
    ev = _run_verify(tmp_path, (
        "def test_a():\n    assert 1 == 1\n"
        "def test_b():\n    assert 2 == 2\n"
        "def test_c():\n    assert 1 == 2\n"
    ))
    msg = ev["checks"][0]["message"]
    assert ev["checks"][0]["passed"] is False
    assert "1 test(s) failed, 2 passed" in msg, msg


def test_t11_f3_collection_error_still_detected(tmp_path):
    ev = _run_verify(tmp_path, "import nonexistent_module_xyz\n")
    msg = ev["checks"][0]["message"]
    assert "collection error" in msg


# ------------------------------------------------------------ torture-11 F4 ----
def test_t11_f4_verify_with_check_sh_timeout_is_caught(tmp_path, monkeypatch):
    from bench import bench_nine as bm

    class _Raiser:
        def __init__(self, *a, **k):
            raise subprocess.TimeoutExpired(cmd="bash", timeout=120)

    monkeypatch.setattr(bm.subprocess, "run", _Raiser)
    patch = tmp_path / "patch.py"
    patch.write_text("x=1\n", encoding="utf-8")
    res = bm.verify_with_check_sh(tmp_path / "check.sh", patch)
    assert res["ran"] is False
    assert res["timed_out"] is True
    assert "timed out" in res["detail"]


# ------------------------------------------------------------ torture-11 F5 ----
def test_t11_f5_subdir_artifact_stale_on_retry(tmp_path):
    """review_multi certifies reviews/*.md — a retry that doesn't rewrite
    them must BLOCK (recursive manifest: subdir files are registered with
    relative names, so stale evidence is caught)."""

    def write_fn(inputs, job_dir):
        jd = Path(job_dir)
        if inputs.get("attempt") == 1:
            (jd / "reviews").mkdir(exist_ok=True)
            (jd / "reviews" / "security.md").write_text("sec", encoding="utf-8")
            (jd / "reviews" / "bugs.md").write_text("bugs", encoding="utf-8")
        else:
            (jd / "FLAG.txt").write_text("ok\n", encoding="utf-8")
        return {"output": "done"}

    wf = Workflow(id="rm")
    wf.add_node(Node(id="n", kind="tool", run=write_fn))
    gate = EvidenceGate()
    gate.register_check("flag", _flag_check)
    gate.register_check("artifacts",
                        required_artifact_check(["reviews/security.md", "reviews/bugs.md"]))
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("rm", {"task": "t"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("t\n", encoding="utf-8")
    res = ex.execute(wf, job, {"task": "t"})
    assert res["verdict"]["verdict"] == "BLOCK"
    assert "reviews/security.md" in res["verdict"]["summary"]


def test_t11_f5_subdir_artifact_rewritten_on_retry_ships(tmp_path):

    def write_fn(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "reviews").mkdir(exist_ok=True)
        (jd / "reviews" / "security.md").write_text(
            "sec v{}".format(inputs.get("attempt")), encoding="utf-8")
        (jd / "reviews" / "bugs.md").write_text("bugs", encoding="utf-8")
        (jd / "FLAG.txt").write_text("ok\n", encoding="utf-8")
        return {"output": "done"}

    wf = Workflow(id="rm")
    wf.add_node(Node(id="n", kind="tool", run=write_fn))
    gate = EvidenceGate()
    gate.register_check("flag", _flag_check)
    gate.register_check("artifacts",
                        required_artifact_check(["reviews/security.md", "reviews/bugs.md"]))
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("rm", {"task": "t"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("t\n", encoding="utf-8")
    res = ex.execute(wf, job, {"task": "t"})
    assert res["verdict"]["verdict"] == "SHIP", res["verdict"]["summary"]
    # recursive manifest registered the subdir files with relative names
    names = {a["name"] for a in ledger.get(job.job_id).artifacts}
    assert "reviews/security.md" in names


def test_t11_f5_directory_artifact_stale_on_retry(tmp_path):
    """build-multi certifies the solution/ DIRECTORY — a retry that writes
    nothing under it must BLOCK (certified by content: stale unless a file
    under it was produced this attempt)."""

    def write_fn(inputs, job_dir):
        jd = Path(job_dir)
        if inputs.get("attempt") == 1:
            (jd / "solution").mkdir(exist_ok=True)
            (jd / "solution" / "main.py").write_text("print(1)\n", encoding="utf-8")
        else:
            (jd / "FLAG.txt").write_text("ok\n", encoding="utf-8")
        return {"output": "done"}

    wf = Workflow(id="bm")
    wf.add_node(Node(id="n", kind="tool", run=write_fn))
    gate = EvidenceGate()
    gate.register_check("flag", _flag_check)
    gate.register_check("artifacts", required_artifact_check(["solution"]))
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("bm", {"task": "t"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("t\n", encoding="utf-8")
    res = ex.execute(wf, job, {"task": "t"})
    assert res["verdict"]["verdict"] == "BLOCK"
    assert "solution/" in res["verdict"]["summary"]


def test_t11_f5_directory_artifact_rewritten_on_retry_ships(tmp_path):

    def write_fn(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "solution").mkdir(exist_ok=True)
        (jd / "solution" / "main.py").write_text(
            "print({})\n".format(inputs.get("attempt")), encoding="utf-8")
        (jd / "FLAG.txt").write_text("ok\n", encoding="utf-8")
        return {"output": "done"}

    wf = Workflow(id="bm")
    wf.add_node(Node(id="n", kind="tool", run=write_fn))
    gate = EvidenceGate()
    gate.register_check("flag", _flag_check)
    gate.register_check("artifacts", required_artifact_check(["solution"]))
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("bm", {"task": "t"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("t\n", encoding="utf-8")
    res = ex.execute(wf, job, {"task": "t"})
    assert res["verdict"]["verdict"] == "SHIP", res["verdict"]["summary"]


# ------------------------------------------------------------ torture-11 F6 ----
def test_t11_f6_bench_exit_code_and_archive(tmp_path, monkeypatch):
    from bench import bench_nine as bm

    fx = tmp_path / "fx"
    fx.mkdir(parents=True)
    (fx / "task.md").write_text("# Eval Fixture: fx\n## Task Description\nmake it\n", encoding="utf-8")
    (fx / "tests").mkdir(parents=True)
    (fx / "tests" / "check.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    (fx / "starter").mkdir()
    starter = "x = 1\n"
    (fx / "starter" / "solution.py").write_text(starter, encoding="utf-8")
    monkeypatch.setattr(bm, "FIXTURES", ["fx"])
    monkeypatch.setattr(bm, "FIXTURES_DIR", tmp_path)
    monkeypatch.setattr(bm, "BENCH_ROOT", tmp_path / "runs")
    monkeypatch.setattr(bm, "RUNID", "harvest7")
    calls = {"n": 0}

    def fake_ok(fixture_dir, workdir, ledger):
        calls["n"] += 1
        return ({"job_id": "j1", "workflow_id": "debug", "router_model": "m",
                 "verdict": "SHIP", "verdict_summary": "ok", "final_status": "shipped",
                 "attempts": 1, "timed_out": False, "cli_error": None}, "", "")

    monkeypatch.setattr(bm, "run_submit", fake_ok)
    rc = bm.main()
    assert rc == 0
    assert (tmp_path / "runs" / "results-harvest7.json").exists()

    def fake_block(fixture_dir, workdir, ledger):
        calls["n"] += 1
        # simulate a BLOCKed fixture that kept the seeded BROKEN starter as
        # its candidate (torture-11 F8: it must NOT be scored on it)
        job_dir = Path(workdir) / "j2"
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "patch.py").write_text(starter, encoding="utf-8")
        return ({"job_id": "j2", "workflow_id": "debug", "router_model": "m",
                 "verdict": "BLOCK", "verdict_summary": "nope", "final_status": "blocked",
                 "attempts": 2, "timed_out": False, "cli_error": None}, "", "")

    monkeypatch.setattr(bm, "run_submit", fake_block)
    rc = bm.main()
    assert rc == 1
    recs = json.loads((tmp_path / "runs" / "results.json").read_text(encoding="utf-8"))
    assert recs[0]["verdict"] == "BLOCK"
    # F8: candidate unchanged from the broken starter -> NOT scored
    assert recs[0]["tests_passed"] == 0 and recs[0]["tests_total"] == 0
    assert recs[0]["candidate_unchanged_from_starter"] is True


# ------------------------------------------------------------ torture-11 F7 ----
def test_t11_f7_convert_warns_on_nested_test_calls(tmp_path, monkeypatch, capsys):
    from bench.bench_nine import convert_to_pytest

    runner = (
        "from implementation import add\n"
        'test("top", lambda: add(1, 2), 3)\n'
        "for i in range(3):\n"
        '    test("loop %d" % i, lambda: add(i, 1), i + 1)\n'
    )
    src = convert_to_pytest(runner)
    err = capsys.readouterr().err
    # 1 top-level call converts; the loop body's single AST call is dropped
    assert "dropped 1" in err
    assert src.count("def test_") == 1


def test_t11_f7_convert_nested_only_raises(tmp_path):
    from bench.bench_nine import convert_to_pytest

    runner = (
        "from implementation import add\n"
        "for i in range(3):\n"
        '    test("loop %d" % i, lambda: add(i, 1), i + 1)\n'
    )
    with pytest.raises(RuntimeError, match="no test"):
        convert_to_pytest(runner)
