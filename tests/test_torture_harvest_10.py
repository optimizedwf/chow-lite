"""Slice 35 harvest tests — torture round 9 (torture-17 8 findings;
torture-18 pending: server/learn/docs findings land in this file too).

All hermetic (no network, no Gemini): real modules + in-process stubs only.
"""
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from nine.gates.evidence import (
    EvidenceGate,
    eval_json_check,
    required_artifact_check,
)
from nine.ledger.ledger import JSONLLedger
from nine.runtime.workflows import Node, Workflow, WorkflowExecutor


def _flag_check(ctx, workdir) -> tuple[bool, str]:
    f = Path(workdir) / "FLAG.txt"
    ok = f.exists() and f.read_text().strip() == "ok"
    return ok, ("flag present" if ok else "FLAG.txt missing")


_flag_check.expected = ["FLAG.txt"]  # type: ignore[attr-defined]  # torture-17 F2 tag


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


# ============================================================== torture-17 ====
def test_t17_f1_toctou_content_swap_blocks_ship(tmp_path):
    """A registered file swapped between manifest registration and the gate
    read (the abandoned-thread/nohup late writer) SHIPs with a manifest
    sha256 that does not match the certified content — the guard must
    re-hash at SHIP time and BLOCK, never ship the lie."""
    wf = Workflow(id="t17f1")

    def write_fn(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "EVAL.json").write_text(json.dumps({
            "checks": [{"name": "c", "passed": False,
                        "message": "not yet"}],
        }), encoding="utf-8")
        return {"output": "ok"}

    wf.add_node(Node(id="n", kind="tool", run=write_fn))

    def _swap(ctx, workdir):
        # simulates the late writer: rewrites the certified file AFTER it
        # was hashed into the manifest (registered with the FAILING bytes).
        (Path(workdir) / "EVAL.json").write_text(json.dumps({
            "checks": [{"name": "c", "passed": True, "message": "ok"}],
        }), encoding="utf-8")
        return True, "swapped"

    _swap.expected = []  # type: ignore[attr-defined]  # certifies nothing on disk

    res, job, jd = _run_workflow(
        tmp_path, wf, {"swap": _swap, "eval-json": eval_json_check()})
    assert res["verdict"]["verdict"] == "BLOCK", res["verdict"]["verdict"]
    assert "content changed during gate evaluation" in res["verdict"]["summary"]

    # control: node writes the PASSING content directly -> manifest hash
    # matches disk at ship time -> SHIP (no false positive).
    wf2 = Workflow(id="t17f1b")

    def write_ok(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "EVAL.json").write_text(json.dumps({
            "checks": [{"name": "c", "passed": True, "message": "ok"}],
        }), encoding="utf-8")
        return {"output": "ok"}

    wf2.add_node(Node(id="n", kind="tool", run=write_ok))
    res2, job2, jd2 = _run_workflow(tmp_path, wf2, {"eval-json": eval_json_check()})
    assert res2["verdict"]["verdict"] == "SHIP", res2["verdict"]["verdict"]


def test_t17_f2_untagged_check_refuses_ship_with_loud_warning(tmp_path, capsys):
    """A custom CheckFn that forgets the .expected tag silently bypassed the
    ENTIRE stale guard (R3: attempt-1 files SHIP on attempt-2 reruns). The
    boundary must warn loudly AND the SHIP branch must refuse BLOCK."""
    wf = Workflow(id="t17f2")

    def write_fn(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "solution.py").write_text("print('hi')\n", encoding="utf-8")
        return {"output": "ok"}

    wf.add_node(Node(id="n", kind="tool", run=write_fn))

    def code_check(ctx, workdir):  # NO .expected — the R3 hole
        p = Path(workdir) / "solution.py"
        return (p.exists() and len(p.read_text()) > 0), "solution present"

    res, job, jd = _run_workflow(tmp_path, wf, {"code": code_check})
    assert res["verdict"]["verdict"] == "BLOCK", res["verdict"]["verdict"]
    assert "without .expected provenance" in res["verdict"]["summary"]
    out = capsys.readouterr()
    assert "no .expected provenance tag" in out.err

    # control: the SAME check tagged declares provenance -> SHIP
    code_check.expected = ["solution.py"]  # type: ignore[attr-defined]
    wf2 = Workflow(id="t17f2b")

    def write_fn2(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "solution.py").write_text("print('hi')\n", encoding="utf-8")
        return {"output": "ok"}

    wf2.add_node(Node(id="n", kind="tool", run=write_fn2))
    res2, job2, jd2 = _run_workflow(tmp_path, wf2, {"code": code_check})
    assert res2["verdict"]["verdict"] == "SHIP", res2["verdict"]["verdict"]


def test_t17_f3_kill_groups_one_field_line_skipped(tmp_path):
    """A one-field pid-file line has NO verifiable spawn epoch — killing on
    the session-leader check alone can SIGKILL an innocent recycled group.
    Skip conservatively; a matching two-field entry is still killed."""
    from bench import bench_nine as bm

    job_dir = tmp_path / "work" / "j1"
    job_dir.mkdir(parents=True)
    pid_file = job_dir / ".nine-node-pids"

    proc = subprocess.Popen(["bash", "-c", "sleep 60"], start_new_session=True)
    try:
        pid_file.write_text(f"{proc.pid}\n", encoding="utf-8")
        assert bm._kill_node_groups(tmp_path / "work") == 0
        assert proc.poll() is None, "one-field pid must NOT be killed"

        real = bm._node_start_epoch(proc.pid)
        assert real is not None
        pid_file.write_text(f"{proc.pid} {real:.3f}\n", encoding="utf-8")
        assert bm._kill_node_groups(tmp_path / "work") >= 1
        proc.wait(timeout=5)
        assert proc.poll() is not None
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def test_t17_f4_kill_groups_garbage_second_field_skips_line(tmp_path):
    """A garbage second field (torn write, node-controlled pid file) must
    skip the LINE — not abort the whole cleanup sweep before later pid
    files are processed."""
    from bench import bench_nine as bm

    bad_dir = tmp_path / "work" / "j1"
    bad_dir.mkdir(parents=True)
    (bad_dir / ".nine-node-pids").write_text(
        "12345 garbage\n", encoding="utf-8")

    proc = subprocess.Popen(["bash", "-c", "sleep 60"], start_new_session=True)
    try:
        good_dir = tmp_path / "work" / "j2"
        good_dir.mkdir(parents=True)
        real = bm._node_start_epoch(proc.pid)
        assert real is not None
        (good_dir / ".nine-node-pids").write_text(
            f"{proc.pid} {real:.3f}\n", encoding="utf-8")
        # the bad line must not abort the sweep: j2's valid entry is killed
        killed = bm._kill_node_groups(tmp_path / "work")
        assert killed >= 1, "valid entry in ANOTHER pid file must still be killed"
        proc.wait(timeout=5)
        assert proc.poll() is not None
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def test_t17_f5_convert_augassign_constant_snapshot(tmp_path):
    """A constant mutated between test() calls (EXPECTED += 1) must inline
    the UPDATED value per call site — the old behavior inlined the stale
    value and inverted the bench verdict both ways."""
    from bench.bench_nine import convert_to_pytest

    runner = (
        "from implementation import f, g\n"
        "EXPECTED = 1\n"
        'test("a", lambda: f(), EXPECTED)\n'
        "EXPECTED += 1\n"
        'test("b", lambda: g(), EXPECTED)\n'
    )
    src = convert_to_pytest(runner)
    assert "== 1" in src, src
    assert "== 2" in src, src
    assert "EXPECTED" not in src, src

    runner2 = (
        "from implementation import f\n"
        "N = 10\n"
        'test("a", lambda: f(), N)\n'
        "N -= 3\n"
        'test("b", lambda: f(), N)\n'
    )
    src2 = convert_to_pytest(runner2)
    assert "== 10" in src2, src2
    assert "== 7" in src2, src2


def test_t17_f6_date_time_requires_time_and_offset():
    """RFC 3339 requires a time component AND a UTC offset — naive,
    date-only, and partial strings must fail validate() at every boundary."""
    from nine.schema_validation import SchemaValidationError, validate

    bad = [
        "2026-08-13",
        "2026-08",
        "2026",
        "2026-08-13T12:00:00",       # naive (no offset)
        "2026-08-13 12:00:00Z",      # space separator (RFC 3339 forbids)
        "2026-08-13t12:00:00z",      # lowercase z is not RFC 3339
    ]
    for v in bad:
        rec = {"verdict": "SHIP", "evidence_refs": [], "verified_at": v,
               "gate_version": "0.1.0"}
        with pytest.raises(SchemaValidationError):
            validate("evidence-verdict", rec)

    for v in ["2026-08-13T12:00:00Z", "2026-08-13t12:00:00+00:00",
              "2026-08-13T12:00:00+00:00"]:
        rec = {"verdict": "SHIP", "evidence_refs": [], "verified_at": v,
               "gate_version": "0.1.0"}
        validate("evidence-verdict", rec)  # must not raise


def test_t17_f7_symlink_to_inside_registered_target_ships(tmp_path):
    """A symlink at an expected input whose TARGET was produced this attempt
    inside the job dir (latest.md -> REPORT.md) certifies REGISTERED content
    — allow it. A symlink to an OUTSIDE target still BLOCKs (T15-F1 core)."""
    wf = Workflow(id="t17f7")

    def write_fn(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "REPORT.md").write_text("REPORT CONTENT\n", encoding="utf-8")
        (jd / "latest.md").symlink_to("REPORT.md")
        return {"output": "ok"}

    wf.add_node(Node(id="n", kind="tool", run=write_fn))

    def latest_check(ctx, workdir):
        p = Path(workdir) / "latest.md"
        return (p.exists() and len(p.read_text()) > 0), "latest present"

    latest_check.expected = ["latest.md"]  # type: ignore[attr-defined]

    res, job, jd = _run_workflow(tmp_path, wf, {"latest": latest_check})
    assert res["verdict"]["verdict"] == "SHIP", res["verdict"]["verdict"]

    # control: symlink to an OUTSIDE file must still BLOCK
    outside = tmp_path / "outside.md"
    outside.write_text("OUTSIDE\n", encoding="utf-8")
    wf2 = Workflow(id="t17f7b")

    def write_fn2(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "REPORT.md").write_text("R\n", encoding="utf-8")
        (jd / "latest.md").symlink_to(str(outside))
        return {"output": "ok"}

    wf2.add_node(Node(id="n", kind="tool", run=write_fn2))
    res2, job2, jd2 = _run_workflow(tmp_path, wf2, {"latest": latest_check})
    assert res2["verdict"]["verdict"] == "BLOCK", res2["verdict"]["verdict"]
    assert "latest.md" in res2["verdict"]["summary"]


def test_t17_f8_outside_same_basename_namespaced(tmp_path):
    """Two DIFFERENT outside files with the same basename (x/report.md and
    y/report.md) must both appear in the manifest — "../<basename>" collided
    and silently dropped one; "../<parent>/<basename>" is unique."""
    x = tmp_path / "x"
    y = tmp_path / "y"
    x.mkdir(parents=True)
    y.mkdir(parents=True)
    (x / "report.md").write_text("X CONTENT\n", encoding="utf-8")
    (y / "report.md").write_text("Y CONTENT\n", encoding="utf-8")

    wf = Workflow(id="t17f8")

    def write_x(inputs, job_dir):
        (Path(job_dir) / "FLAG.txt").write_text("ok\n", encoding="utf-8")
        return {"output": "ok", "artifact_path": str(x / "report.md")}

    def write_y(inputs, job_dir):
        return {"output": "ok", "artifact_path": str(y / "report.md")}

    wf.add_node(Node(id="nx", kind="tool", run=write_x))
    wf.add_node(Node(id="ny", kind="tool", run=write_y))
    res, job, jd = _run_workflow(
        tmp_path, wf, {"flag": _flag_check,
                       "artifacts": required_artifact_check(["FLAG.txt"])})
    assert res["verdict"]["verdict"] == "SHIP", res["verdict"]["verdict"]
    names = {a["name"] for a in job.artifacts}
    # T19-F2 (slice 37): namespace is now "../<parent>-<8-hex digest>/<name>"
    # so outside files whose parents share a NAME (x/a/report.md vs
    # y/a/report.md) still cannot collide — assert the essential property:
    # BOTH files present, distinct names, each pointing at its own content.
    x_names = [n for n in names if n.startswith("../x-") and n.endswith("/report.md")]
    y_names = [n for n in names if n.startswith("../y-") and n.endswith("/report.md")]
    assert len(x_names) == 1, names
    assert len(y_names) == 1, names
    assert x_names != y_names, names

# ============================================================== torture-18 ====

class _FakeFirestore:
    """Minimal firestore-shaped fake: collection()/document().set()."""

    def __init__(self):
        self.docs = {}

    def collection(self, name):
        return _FakeFirestore._Col(self, name)

    class _Col:
        def __init__(self, db, name):
            self.db, self.name = db, name

        def document(self, job_id):
            return _FakeFirestore._Doc(self.db, f"{self.name}/{job_id}")

    class _Doc:
        def __init__(self, db, path):
            self.db, self.path = db, path

        def set(self, data, merge=False):
            self.db.docs[self.path] = data


def test_t18_f1_firestore_submit_redacts_and_validates():
    """torture-18 F1: FirestoreLedger.submit bypassed redact()/validate() —
    the Cloud Run backend (the PRODUCTION ledger, preferred by get_ledger)
    wrote RAW task text (AKIA/sk-live/password values the user pasted)
    verbatim into Firestore and accepted records agent-job validation
    rejects. Mirror JSONLLedger: redact at the boundary, validate first."""
    from nine.ledger.firestore_ledger import FirestoreLedger
    from nine.schema_validation import SchemaValidationError

    TASK = ("deploy with AKIAIOSFODNN7EXAMPLE and "
            "sk-live-abcdefghijklmnopqrstuvwxyz0123456789 and password=hunter3")
    db = _FakeFirestore()
    fs = FirestoreLedger.__new__(FirestoreLedger)
    fs.db, fs.collection = db, "nine-jobs"
    job = fs.submit("research", {"task": TASK})
    stored = list(db.docs.values())[0]
    assert TASK not in str(stored["input"]["task"]), "raw task hit Firestore"
    assert "AKIAIOSFODNN7EXAMPLE" not in str(stored["input"]["task"])
    assert "sk-live-" not in str(stored["input"]["task"])
    assert job.input["task"] == stored["input"]["task"]  # same redacted copy
    assert "hunter3" not in str(stored)

    # validation runs before .set(): a record agent-job rejects never lands
    fs2 = FirestoreLedger.__new__(FirestoreLedger)
    fs2.db, fs2.collection = _FakeFirestore(), "x"
    with pytest.raises(SchemaValidationError):
        fs2.submit(123, {"task": "t"})  # non-string workflow_id


def test_t18_f2_server_skips_cancelled_route_event():
    """torture-18 F2: _record_route_event lacked the CLI's CANCELLED skip —
    an operator-cancelled job turned POST /v1/submit into a raw 500
    (route-event schema admits no CANCELLED). The skip must be duplicated
    on the server surface."""
    import deploy.server as server

    class _Recorder:
        def __init__(self):
            self.events = []

        def observe(self, ev):
            self.events.append(ev)

    from nine.router.classifier import RouteDecision

    decision = RouteDecision(
        decision_id="d1", task_redacted="t", workflow_id="respond",
        confidence=0.9, reason="r", decided_at="2026-01-01T00:00:00+00:00",
        router_version="v")
    cancelled = {"verdict": "CANCELLED", "evidence_refs": [],
                 "eval_results": {}, "summary": "op cancelled",
                 "verified_at": "2026-01-01T00:00:00+00:00",
                 "gate_version": None}
    rec = _Recorder()
    server._record_route_event(rec, None, decision, cancelled)
    assert rec.events == [], "CANCELLED must not reach the route-event store"

    shipped = {"verdict": "SHIP", "evidence_refs": ["RESPONSE.md"],
               "eval_results": {}, "summary": "s",
               "verified_at": "2026-01-01T00:00:00+00:00",
               "gate_version": "0.1.0"}
    server._record_route_event(rec, None, decision, shipped)
    assert len(rec.events) == 1 and rec.events[0].verdict == "SHIP"


def test_t18_f3_cancelled_verdict_is_durable(tmp_path):
    """torture-18 F3: _abort_cancelled returned the CANCELLED verdict to
    the caller but NEVER called ledger.update(job) — the durable ledger
    ended with verdicts: [] and the cancel reason/artifacts vanished on
    process exit. The cancelled-status row must carry the full verdict."""
    import threading
    import time

    from nine.gates.evidence import required_artifact_check
    from nine.runtime.workflows import WorkflowExecutor

    led = JSONLLedger(tmp_path / "ledger.jsonl")
    wf = Workflow(id="cx")
    wf.add_node(Node(id="n", kind="bash",
                     command="echo ok > out.txt && sleep 5"))
    gate = EvidenceGate()
    gate.register_check("artifacts",
                        required_artifact_check(["out.txt"]))
    ex = WorkflowExecutor(led, gate, workdir=tmp_path / "work")
    job = led.submit(wf.id, {"task": "t"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("t\n", encoding="utf-8")

    holder = {}

    def _run():
        holder["res"] = ex.execute(wf, job, {"task": "t"})

    th = threading.Thread(target=_run)
    th.start()
    time.sleep(1.2)
    led.cancel(job.job_id)  # operator cancel, cross-process style
    th.join(timeout=60)
    assert not th.is_alive()

    res = holder["res"]
    assert res["verdict"]["verdict"] == "CANCELLED", res["verdict"]
    rows = [json.loads(line) for line in
            (tmp_path / "ledger.jsonl").read_text().splitlines()
            if line.strip()]
    rows = [r for r in rows if r["job_id"] == job.job_id]
    last = rows[-1]
    assert last["status"] == "cancelled", last["status"]
    verdicts = last.get("verdicts", [])
    assert any(v.get("verdict") == "CANCELLED" for v in verdicts), verdicts
    cancelled_row = next(v for v in verdicts
                         if v.get("verdict") == "CANCELLED")
    assert "evidence_refs" in cancelled_row
    assert cancelled_row["summary"]
    assert cancelled_row["gate_version"] is None
    # no row after the cancel may stamp shipped/blocked over it
    first_cancel = next(i for i, r in enumerate(rows)
                        if r["status"] == "cancelled")
    assert all(r["status"] == "cancelled" for r in rows[first_cancel:]), \
        [r["status"] for r in rows]


def test_t18_f4_apply_revert_retry_refuses_uncommitted(tmp_path, monkeypatch,
                                                      capsys):
    """torture-18 F4: the "already in catalog"/"not present" retry branches
    flipped candidate status WITHOUT committing — a retry after a failed
    T16-F9 commit marked applied/pending while the audit commit never
    landed. Only a COMMITTED on-disk catalog may flip status."""
    import nine.registry as reg
    from nine import cli as nine_cli
    from nine.learn.learner import (
        ImprovementCandidate,
        Learner,
        RouteEventStore,
    )

    # hermetic catalog: swap load/save for a tmp-backed dict
    state = {"keyword_overrides": {"research": []}}

    def _load():
        return json.loads(json.dumps(state))

    def _save(cat):
        state.clear()
        state.update(json.loads(json.dumps(cat)))

    monkeypatch.setattr(reg, "load_catalog", _load)
    monkeypatch.setattr(reg, "save_catalog", _save)
    monkeypatch.setattr(nine_cli, "_regression_green", lambda: True)
    monkeypatch.setattr(nine_cli, "_git_commit", lambda msg: False)

    lr = Learner(RouteEventStore(tmp_path / "events.jsonl"))
    cand = ImprovementCandidate(
        candidate_id="c1", kind="keyword", description="d", evidence=[],
        status="pending",
        params={"workflow_id": "research", "keyword": "researchfy"})
    lr.cands.append(cand)

    # apply#1: commit fails (T16-F9) — catalog mutated on disk, rc 1
    assert nine_cli._apply_candidate(lr, "c1") == 1
    assert lr.cands.get("c1").status == "pending"
    assert "researchfy" in state["keyword_overrides"]["research"]

    # apply#2 (retry, nothing fixed): old code marked APPLIED with no
    # commit — new code refuses while the catalog is uncommitted
    monkeypatch.setattr(nine_cli, "_catalog_is_committed", lambda: False)
    assert nine_cli._apply_candidate(lr, "c1") == 1
    assert lr.cands.get("c1").status == "pending"
    err = capsys.readouterr().err
    assert "NOT marked applied" in err

    # operator commits manually -> retry flips applied
    monkeypatch.setattr(nine_cli, "_catalog_is_committed", lambda: True)
    assert nine_cli._apply_candidate(lr, "c1") == 0
    assert lr.cands.get("c1").status == "applied"

    # revert symmetric: commit fails -> catalog already lacks kw, status
    # must NOT flip to pending while uncommitted
    monkeypatch.setattr(nine_cli, "_git_commit", lambda msg: False)
    monkeypatch.setattr(nine_cli, "_catalog_is_committed", lambda: False)
    # remove the kw from the hermetic catalog first (revert#1's mutation)
    state["keyword_overrides"]["research"].remove("researchfy")
    assert nine_cli._revert_candidate(lr, "c1") == 1
    assert lr.cands.get("c1").status == "applied"
    err2 = capsys.readouterr().err
    assert "NOT marked pending" in err2

    monkeypatch.setattr(nine_cli, "_catalog_is_committed", lambda: True)
    assert nine_cli._revert_candidate(lr, "c1") == 0
    assert lr.cands.get("c1").status == "pending"


def _submit_client(tmp_path, monkeypatch, router, executor=None):
    """Fresh TestClient against a hermetic server state (tmp JSONL ledger,
    fake router, optional fake executor/chain registry)."""
    from fastapi.testclient import TestClient

    import deploy.server as server
    import nine.registry as reg
    from nine.learn.learner import Learner, RouteEventStore
    from nine.ledger.ledger import JSONLLedger

    (tmp_path / "jobs").mkdir(exist_ok=True)
    monkeypatch.setattr(server, "WORKDIR", tmp_path / "work")
    monkeypatch.setattr(server, "LEDGER_PATH", tmp_path / "jobs" / "ledger.jsonl")
    monkeypatch.setattr(server, "EVENTS_PATH", tmp_path / "jobs" / "events.jsonl")
    monkeypatch.setattr(server, "MEMORY_PATH", tmp_path / "jobs" / "memory.jsonl")
    monkeypatch.setattr(server, "get_ledger",
                        lambda: JSONLLedger(server.LEDGER_PATH))
    monkeypatch.setattr(server, "get_learner",
                        lambda: Learner(RouteEventStore(server.EVENTS_PATH)))
    monkeypatch.setattr(server, "build_router", lambda: router)
    if executor is not None:
        monkeypatch.setattr(server, "WorkflowExecutor", executor)
    monkeypatch.setattr(reg, "workflow_gate", lambda wid: None)
    monkeypatch.setattr(server, "get_memory", lambda: None)
    return TestClient(server.app)


def test_t18_f5_server_submit_clean_502s(tmp_path, monkeypatch):
    """torture-18 F5: server submit raw-500'd on (a) job-dir preparation
    (WORKDIR-as-file -> FileExistsError/NotADirectoryError) and (b) an
    uncaught ChainError; and (c) the workflows path opened get_memory()
    AFTER ledger.submit (a broken memory store 502'd an already-committed
    job). All three now return the clean LedgerUnavailable/ChainError
    contract."""
    import nine.registry as reg
    from nine.chains.chain import ChainError
    from nine.router.classifier import RouteDecision

    def decision(wid):
        return RouteDecision(
            decision_id="d1", task_redacted="t", workflow_id=wid,
            confidence=0.9, reason="r",
            decided_at="2026-01-01T00:00:00+00:00", router_version="v")

    class _Router:
        def __init__(self, wid):
            self.wid = wid

        def classify(self, task):
            return decision(self.wid)

    class _FakeChain:
        pass

    # (a) WORKDIR is a FILE -> job_dir.mkdir raises OSError -> clean 502
    work_file = tmp_path / "workfile"
    work_file.write_text("x", encoding="utf-8")
    monkeypatch.setattr(reg, "CHAINS", {"fake-chain": _FakeChain})
    client = _submit_client(tmp_path, monkeypatch, _Router("fake-chain"))
    monkeypatch.setattr(__import__("deploy.server", fromlist=["server"]),
                        "WORKDIR", work_file)
    r = client.post("/v1/submit", json={"task": "t"})
    assert r.status_code == 502, r.status_code
    assert "job dir" in r.json()["detail"] or "prepare chain" in r.json()["detail"]

    # (b) ChainError escapes a chain hop -> clean 502 (new handler)
    from types import SimpleNamespace

    boom_wf = Workflow(id="h1w")

    def _boom_run(inputs, job_dir):
        raise ChainError("hop exploded")

    boom_wf.add_node(Node(id="n", kind="tool", run=_boom_run))
    _BoomChain = type("_BoomChain", (), {
        "id": "fake-chain",
        "hops": [SimpleNamespace(id="h1", max_fix_loops=0,
                                 workflow=boom_wf, gate_checks={})]})
    monkeypatch.setattr(reg, "CHAINS", {"fake-chain": _BoomChain})
    client2 = _submit_client(tmp_path, monkeypatch, _Router("fake-chain"))
    r = client2.post("/v1/submit", json={"task": "t"})
    assert r.status_code == 502, r.status_code
    assert "hop exploded" in r.json()["detail"]

    # (c) workflows path must NOT open the memory store (order-of-ops):
    # a broken memory backend must not 502 an already-committed workflow job
    class _FakeEx:
        def __init__(self, *a, **k):
            pass

        def execute(self, wf, job, inputs):
            job.status = "shipped"
            return {"verdict": {"verdict": "SHIP", "evidence_refs": [],
                                "eval_results": {}, "summary": "s",
                                "verified_at": "2026-01-01T00:00:00+00:00",
                                "gate_version": "0.1.0"},
                    "attempts": 1}

    class _FakeWF:
        id = "fake-wf"

    import deploy.server as server

    def _memory_boom():
        raise AssertionError("get_memory called on the WORKFLOWS path")

    monkeypatch.setattr(server, "get_memory", _memory_boom)
    monkeypatch.setattr(server, "WorkflowExecutor", _FakeEx)
    monkeypatch.setattr(reg, "WORKFLOWS", {"fake-wf": _FakeWF})
    client3 = _submit_client(tmp_path, monkeypatch, _Router("fake-wf"),
                             executor=_FakeEx)
    r = client3.post("/v1/submit", json={"task": "t"})
    assert r.status_code == 200, (r.status_code, r.text)
    assert r.json()["verdict"]["verdict"] == "SHIP"


def test_t18_f6_redact_covers_underscore_key_forms():
    """torture-18 F6: redact() passed *_key = <value> through verbatim —
    private_key, public_key, consumer_key, access_key, client_key,
    secret_key, ssh_key all leaked. The value alternation must cover the
    underscore-key family; innocent bare-word uses stay untouched."""
    from nine.router.classifier import redact

    leaks = [
        "private_key = super-secret-value-12345",
        "client_secret_key = abcdefghijklmnopqrstuvwxyz123456",
        "consumer_key = 0123456789abcdefghijklmnopqrstuv",
        "access_key = ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789",
        "ssh_key = abcdefghijklmnopqrstuvwxyz0123456789",
        "public_key = MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A",
        "client_private_key: hunter2",
        "secret_key == abc == def",
        '{"private_key": "sk-123 abc def"}',
    ]
    for text in leaks:
        out = redact(text)
        assert "***" in out, (text, out)
    controls = ["the key is on the table", "skillfulness of the model",
                "task list", "ghost town"]
    for text in controls:
        assert redact(text) == text, text


def test_t18_f7_gate_version_type_enforced():
    """torture-18 F7: the evidence-verdict schema only REQUIRED gate_version
    for non-CANCELLED verdicts — `gate_version: null` on a SHIP/FIX/BLOCK
    passed (presence-only). Non-CANCELLED verdicts must carry a STRING
    gate_version; CANCELLED stays free (documented null)."""
    from nine.schema_validation import SchemaValidationError, validate

    base = {"verdict": "SHIP", "evidence_refs": [],
            "verified_at": "2026-01-01T00:00:00+00:00"}
    with pytest.raises(SchemaValidationError):
        validate("evidence-verdict",
                 {**base, "gate_version": None})
    validate("evidence-verdict", {**base, "gate_version": "0.1.0"})
    validate("evidence-verdict",
             {"verdict": "CANCELLED", "evidence_refs": [],
              "verified_at": "2026-01-01T00:00:00+00:00",
              "gate_version": None})


def test_t18_f8_boundary_schemas_strict_and_terminal_guard(tmp_path):
    """torture-18 F8: boundary schemas had no additionalProperties: false —
    a typo key sailed through every validate() call. And add_verdict had no
    shipped-job guard: a late non-CANCELLED verdict mutated a closed audit
    record. Both are now enforced."""
    from nine.ledger.ledger import LedgerError
    from nine.schema_validation import SchemaValidationError, validate

    with pytest.raises(SchemaValidationError):
        validate("agent-job",
                 {"job_id": "j", "workflow_id": "research",
                  "status": "submitted", "created_at": "2026-01-01T00:00:00+00:00",
                  "updated_at": "2026-01-01T00:00:00+00:00",
                  "typo_key": True})
    with pytest.raises(SchemaValidationError):
        validate("artifact-manifest",
                 {"name": "a", "path": "a.txt", "kind": "file",
                  "sha256": "h", "size": 1, "produced_by": "n",
                  "produced_at": "2026-01-01T00:00:00+00:00",
                  "typo_key": True})
    with pytest.raises(SchemaValidationError):
        validate("evidence-verdict",
                 {"verdict": "SHIP", "evidence_refs": [],
                  "verified_at": "2026-01-01T00:00:00+00:00",
                  "gate_version": "0.1.0", "typo_key": True})
    with pytest.raises(SchemaValidationError):
        validate("route-decision",
                 {"decision_id": "d", "task_redacted": "t",
                  "workflow_id": "research", "confidence": 0.9,
                  "reason": "r", "decided_at": "2026-01-01T00:00:00+00:00",
                  "router_version": "v", "typo_key": True})
    with pytest.raises(SchemaValidationError):
        validate("route-event",
                 {"event_id": "e", "job_id": "j", "task_redacted": "t",
                  "workflow_id": "research", "confidence": 0.9,
                  "router_version": "v", "verdict": "SHIP",
                  "checks_passed": 1, "checks_total": 1,
                  "recorded_at": "2026-01-01T00:00:00+00:00",
                  "typo_key": True})

    # add_verdict guard: a terminal job refuses late non-CANCELLED verdicts
    led = JSONLLedger(tmp_path / "ledger.jsonl")
    job = led.submit("research", {"task": "t"})
    job.status = "shipped"
    with pytest.raises(LedgerError):
        job.add_verdict({"verdict": "SHIP", "evidence_refs": [],
                         "verified_at": "2026-01-01T00:00:00+00:00",
                         "gate_version": "0.1.0"})
    # CANCELLED stays legal on a terminal job (recover-gate marker, T16-F1)
    job.add_verdict({"verdict": "CANCELLED", "evidence_refs": [],
                     "verified_at": "2026-01-01T00:00:00+00:00",
                     "gate_version": None})
    assert job.verdicts[-1]["verdict"] == "CANCELLED"


# ============================================================== torture-20 ====

def test_t20_f1_all_router_workflow_ids_have_gates():
    """torture-20 F1: several hop factories were registered in WORKFLOWS but
    missing from _HOPS, so resolve_gate() fell back to the GENERIC gate and
    those lanes shipped without certifying their own artifacts. Every
    router-selectable workflow id must resolve to a real per-hop gate."""
    from nine import registry

    wf_ids = set(registry.WORKFLOWS)
    assert wf_ids, "registry must expose workflows"
    for wid in sorted(wf_ids):
        gate = registry.resolve_gate(wid)
        assert gate is not None, f"resolve_gate({wid!r}) is None"
        assert gate.checks, f"resolve_gate({wid!r}) has no checks"
    # respond is the one id with its own respond_gate (no _HOPS factory);
    # every OTHER id must be present so no lane silently falls back.
    uncovered = wf_ids - {"respond"} - set(registry._HOPS)
    assert not uncovered, f"workflow ids without hop gates: {sorted(uncovered)}"


def test_t20_f2_resolve_gate_cli_and_server_share_single_dispatch():
    """torture-20 F2: the CLI and deploy/server.py each had their own generic
    fallback gate, and server.py kept a DEAD `gate = build_gate()` assignment
    beside the live verdict path — a reader could mistake the dead gate for
    the verdict path. Both surfaces must reach nine.registry.resolve_gate
    (one expression, one verdict per evidence set)."""
    import inspect

    import deploy.server as server
    from nine import cli as nine_cli
    from nine.registry import resolve_gate

    assert "resolve_gate" in inspect.getsource(server)
    assert "resolve_gate" in inspect.getsource(nine_cli)
    assert "gate = build_gate()" not in inspect.getsource(server), (
        "dead build_gate assignment must be gone (T20-F2)")
    # the workflow gate and the generic gate genuinely differ for build:
    # the build lane certifies produced artifacts, the generic gate only
    # eval-json + exit-codes. A build job dir with no produced artifacts
    # must FIX under resolve_gate("build") but SHIP under the generic gate.
    build_gate = resolve_gate("build")
    generic_gate = resolve_gate("__no_such_workflow__")
    assert set(generic_gate.checks) == {"eval-json", "exit-codes"}
    assert "artifacts" in build_gate.checks
    assert set(build_gate.checks) - set(generic_gate.checks), (
        "build gate must be stricter than the generic gate")


def test_t20_f3_failed_loud_records_failed_route_event(tmp_path):
    """torture-20 F3: a failed-loud run is a route observation — the LEARN
    loop must see failures, not just SHIPs. The route-event schema must
    admit FAILED and RouteEventStore.record must persist it (before the fix
    'FAILED' failed schema validation and the event was silently dropped)."""
    from nine.learn.learner import RouteEvent, RouteEventStore

    store = RouteEventStore(tmp_path / "events.jsonl")
    store.record(RouteEvent(
        event_id="e1", job_id="j1", task_redacted="t",
        workflow_id="build", confidence=0.9, router_version="v",
        verdict="FAILED", checks_passed=0, checks_total=0,
    ))
    events = store.all()
    assert len(events) == 1 and events[0].verdict == "FAILED", events

    # server surface: _record_route_event persists FAILED too
    import deploy.server as server
    from nine.router.classifier import RouteDecision

    decision = RouteDecision(
        decision_id="d1", task_redacted="t", workflow_id="respond",
        confidence=0.9, reason="r", decided_at="2026-01-01T00:00:00+00:00",
        router_version="v")

    class _Recorder:
        def __init__(self):
            self.events = []

        def observe(self, ev):
            self.events.append(ev)

    rec = _Recorder()
    server._record_route_event(rec, None, decision,
                               {"verdict": "FAILED", "eval_results": {}})
    assert len(rec.events) == 1 and rec.events[0].verdict == "FAILED"


def test_t20_f5_provenance_tags_set_at_factory_time():
    """torture-20 F5: gate factories must tag .expected BEFORE returning so
    register_check never warns and the stale-artifact guard can prove what
    each check certifies (untagged checks refuse SHIP with a loud warning —
    T17-F2 — so a factory forgetting the tag breaks every lane at runtime)."""
    import contextlib
    import io

    from nine.gates.evidence import (
        eval_json_check,
        exit_codes_check,
        file_nonempty_check,
        required_artifact_check,
    )

    assert eval_json_check().expected == ["EVAL.json"]
    assert file_nonempty_check("RESPONSE.md").expected == ["RESPONSE.md"]
    assert required_artifact_check(["a.txt", "b.txt"]).expected == [
        "a.txt", "b.txt"]
    assert exit_codes_check().expected == []

    # registering the tagged factories emits NO warning
    g = EvidenceGate()
    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        g.register_check("eval-json", eval_json_check())
        g.register_check("exit-codes", exit_codes_check())
    assert buf.getvalue() == ""


def test_t20_f6_discover_status_validation_and_short_token_redaction(tmp_path):
    """torture-20 F6: `nine discover --status BOGUS` must fail loudly with a
    clean error (not silently enumerate), and short credential tokens
    (sk-123, AIza123) must be redacted by redact()."""
    from nine.router.classifier import redact

    out = redact("key=sk-123 and token AIza123")
    assert "sk-123" not in out and "AIza123" not in out
    assert "***" in out

    r = subprocess.run(
        [sys.executable, "-m", "nine.cli", "discover", "--status", "BOGUS"],
        capture_output=True, text=True, cwd=tmp_path, check=False,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent)},
    )
    assert r.returncode != 0, r.stdout
    assert "error" in (r.stderr + r.stdout).lower()


# ============================================================== torture-19 ====

def test_t19_f1_symlink_expected_rehashes_resolved_target(tmp_path):
    """torture-19 F1 (HIGH): a check whose .expected names a SYMLINK
    (latest.md -> REPORT.md) skipped the T17-F1 re-hash entirely — symlinks
    are never registered in the manifest, so the guard's ref lookup returned
    None and the loop moved on, letting a late writer swap the TARGET and
    SHIP a manifest whose sha256 never matched disk. The guard must re-hash
    the RESOLVED target against the target's manifest entry."""
    wf = Workflow(id="t19f1")

    def write_fn(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "REPORT.md").write_text("REPORT CONTENT\n", encoding="utf-8")
        (jd / "latest.md").symlink_to("REPORT.md")
        return {"output": "ok"}

    wf.add_node(Node(id="n", kind="tool", run=write_fn))

    def latest_check(ctx, workdir):
        p = Path(workdir) / "latest.md"
        return (p.exists() and len(p.read_text()) > 0), "latest present"

    latest_check.expected = ["latest.md"]  # type: ignore[attr-defined]

    def _swap_target(ctx, workdir):
        # late writer rewrites the RESOLVED TARGET after the manifest was
        # hashed: latest.md now reads the swapped bytes.
        (Path(workdir) / "REPORT.md").write_text(
            "SWAPPED CONTENT\n", encoding="utf-8")
        return True, "swapped"

    _swap_target.expected = []  # type: ignore[attr-defined]

    res, job, jd = _run_workflow(
        tmp_path, wf,
        {"latest": latest_check, "swap": _swap_target})
    assert res["verdict"]["verdict"] == "BLOCK", res["verdict"]["summary"]
    assert "content changed during gate evaluation" in res["verdict"]["summary"]

    # control: no tamper -> target hash matches -> SHIP (no false positive)
    wf2 = Workflow(id="t19f1b")
    wf2.add_node(Node(id="n", kind="tool", run=write_fn))
    res2, job2, jd2 = _run_workflow(
        tmp_path, wf2, {"latest": latest_check})
    assert res2["verdict"]["verdict"] == "SHIP", res2["verdict"]["summary"]


def test_t19_f2_outside_same_parent_name_namespaced(tmp_path):
    """torture-19 F2: two DIFFERENT outside roots whose immediate parents
    share a NAME (x/a/report.md vs y/a/report.md) both mapped to
    "../a/report.md" — one manifest entry silently REPLACED the other and
    the survivor resolved to a third location. The namespace must include a
    digest of the RESOLVED parent so same-named parents cannot collide."""
    x = tmp_path / "x" / "a"
    y = tmp_path / "y" / "a"
    x.mkdir(parents=True)
    y.mkdir(parents=True)
    (x / "report.md").write_text("X CONTENT\n", encoding="utf-8")
    (y / "report.md").write_text("Y CONTENT\n", encoding="utf-8")

    wf = Workflow(id="t19f2")

    def write_x(inputs, job_dir):
        (Path(job_dir) / "FLAG.txt").write_text("ok\n", encoding="utf-8")
        return {"output": "ok", "artifact_path": str(x / "report.md")}

    def write_y(inputs, job_dir):
        return {"output": "ok", "artifact_path": str(y / "report.md")}

    wf.add_node(Node(id="nx", kind="tool", run=write_x))
    wf.add_node(Node(id="ny", kind="tool", run=write_y))
    res, job, jd = _run_workflow(
        tmp_path, wf,
        {"flag": _flag_check,
         "artifacts": required_artifact_check(["FLAG.txt"])})
    assert res["verdict"]["verdict"] == "SHIP", res["verdict"]["summary"]
    by_name = {a["name"]: a for a in job.artifacts}
    x_names = [n for n in by_name
               if n.startswith("../a-") and n.endswith("/report.md")]
    assert len(x_names) == 2, list(by_name)
    assert x_names[0] != x_names[1], "same-parent-name roots must not collide"
    assert all(a["sha256"] for a in job.artifacts), "artifacts lack sha256"


def test_t19_f3_redact_hyphen_camelcase_flag_urlencoded():
    """torture-19 F3: redact() only matched underscore key spellings, so
    --private-key=hunter2, "clientKey": "abc123", ssh-key xyz and
    api_key%3Dsupersecret reached the durable ledger raw. The shared _KEY
    alternation must cover hyphen/space/camelCase plus --flag=value and
    urlencoded separators — while the documented controls stay untouched."""
    from nine.router.classifier import redact

    cases = [
        ("--private-key=hunter2", True),
        ('"private-key": "hunter2"', True),
        ('"privateKey": "hunter2"', True),
        ('"clientKey": "abc123"', True),
        ("ssh-key xyz", True),
        ("api_key%3Dsupersecret", True),
        ("privateKey=ghp_" + "abcdefgh123456", True),
        ("client_secret_key = abcdef123456", True),
        ("the key is on the table", False),
        ("skillfulness of the model", False),
        ("task list", False),
        ("ghost town", False),
        ("api_key=sk-" + "1234567890abcdef", True),   # old underscore form
        ("password == hunter2 == hunter3", True),  # T16-F4 chains intact
        ("xox" + "b-1234567890-abcdefghijklmnop", True),  # slack intact (literal split so GitHub secret scanning does not flag the fixture)
    ]
    for text, want in cases:
        out = redact(text)
        assert ("***" in out) == want, f"{text!r} -> {out!r} (want {want})"


def test_t19_f4_nan_epoch_skipped_by_identity_gate(tmp_path):
    """torture-19 F4: bench_nine read `{pid} nan` from .nine-node-pids —
    float("nan") never raises, abs(actual - start) > 1.0 is always False
    with nan, so the spawn-epoch identity gate passed and the sweeper
    SIGTERM/SIGKILLed a process group it could NOT prove was the node's.
    Non-finite epochs must be skipped (like the T17-F3 one-field rule)."""
    from bench import bench_nine as bm

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                            start_new_session=True)
    workdir = tmp_path / "work"
    workdir.mkdir(parents=True)
    (workdir / ".nine-node-pids").write_text(
        f"{proc.pid} nan\n", encoding="utf-8")
    try:
        killed = bm._kill_node_groups(workdir)
        assert killed == 0, f"nan epoch must not kill: killed={killed}"
        assert proc.poll() is None, "process was killed despite nan epoch"
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def test_t19_f5_nonconstant_reassignment_drops_stale_constant():
    """torture-19 F5: _constant_snapshots kept a stale literal when a
    constant-name was REASSIGNED to a non-constant expression
    (EXPECTED = 5; EXPECTED = add(1,2)) — the fixture inlined `== 5` and
    went false red/green against the real value. Reassignment to a
    non-literal must drop the snapshot (mirroring the Delete branch), and
    the dangling name must raise loudly instead of silently inlining."""
    from bench.bench_nine import convert_to_pytest

    # EXPECTED is a literal at first, then REASSIGNED to a call: the stale
    # literal must NOT be inlined (fixture would assert == 5 forever); the
    # name drops out of the snapshot and conversion fails LOUDLY (dangling)
    # instead of emitting a false red/green suite.
    runner = (
        "from implementation import add\n"
        "EXPECTED = 5\n"
        "EXPECTED = add(1, 2)\n"
        'test("x", lambda: add(1, 2), EXPECTED)\n'
    )
    with pytest.raises(RuntimeError, match="not importable"):
        convert_to_pytest(runner)

    # control 1: a plain literal constant still inlines (== 5)
    src_ok = (
        "from implementation import add\n"
        "EXPECTED = 5\n"
        'test("x", lambda: add(1, 2), EXPECTED)\n'
    )
    out_ok = convert_to_pytest(src_ok)
    assert "== 5" in out_ok, out_ok

    # control 2: reassignment to ANOTHER literal snapshots the new value
    src_re = (
        "from implementation import add\n"
        "EXPECTED = 5\n"
        "EXPECTED = 6\n"
        'test("x", lambda: add(1, 2), EXPECTED)\n'
    )
    out_re = convert_to_pytest(src_re)
    assert "== 6" in out_re and "== 5" not in out_re, out_re


def test_t19_f6_firestore_recover_refresh_contract():
    """torture-19 F6: FirestoreLedger.recover silently no-op'd on a
    `submitted` job (JSONLLedger raises LedgerError), and the class had no
    refresh() nor _jobs cache — so `nine recover --force` hit
    cli.py's ledger._jobs[...] AttributeError in production. The API must
    mirror JSONLLedger: refresh() = durable read, get() populates the
    cache, recover() raises unless blocked/failed and resets attempts."""
    from nine.ledger.firestore_ledger import FirestoreLedger
    from nine.ledger.ledger import LedgerError

    store = {}
    rec = {"workflow_id": "build", "job_id": "j1", "status": "submitted",
           "created_at": "2026-08-13T12:00:00Z",
           "updated_at": "2026-08-13T12:00:00Z", "attempts": 1,
           "artifacts": []}
    store["j1"] = dict(rec)

    class FakeDoc:
        def __init__(self, rec):
            self.rec = rec
            self.exists = rec is not None

        def to_dict(self):
            return self.rec

    class FakeRef:
        def __init__(self, jid):
            self.jid = jid

        def get(self):
            return FakeDoc(store.get(self.jid))

        def update(self, fields):
            store[self.jid].update(fields)

    fl = object.__new__(FirestoreLedger)
    fl._jobs = {}
    fl._ref = FakeRef  # type: ignore[attr-defined]

    # refresh() is a durable read (status submitted as stored)
    j = fl.refresh("j1")
    assert j.status == "submitted" and j.job_id == "j1"
    assert fl._jobs.get("j1") is j, "refresh() must populate the _jobs cache"

    # recover() of a submitted job RAISES (was a silent no-op)
    with pytest.raises(LedgerError):
        fl.recover("j1")

    # recover() of a blocked job transitions + resets attempts, durably
    store["j1"]["status"] = "blocked"
    store["j1"]["attempts"] = 3
    j = fl.recover("j1")
    assert j.status == "recovered" and j.attempts == 0
    assert store["j1"]["status"] == "recovered"
    assert store["j1"]["attempts"] == 0


def test_t19_f7_date_time_rejects_non_rfc3339_offsets():
    """torture-19 F7 (LOW): _check_date_time accepted non-RFC-3339 offsets
    (+00, +0530 — RFC 3339 requires ±HH:MM). Ship a timestamp with a
    colon-less offset and the schema must reject it; well-formed Z /
    ±HH:MM / fractional offsets must still pass."""
    from nine import schema_validation as sv

    check = sv._FORMAT_CHECKER.checkers["date-time"][0]

    for bad in ("2026-01-01T00:00:00+00",
                "2026-01-01T00:00:00-00",
                "2026-01-01T00:00:00+01",
                "2026-01-01T00:00:00+0530",
                "2026-01-01T00:00:00-0530"):
        assert not check(bad), f"{bad!r} must be rejected"

    for good in ("2026-01-01T00:00:00Z",
                 "2026-01-01T00:00:00+00:00",
                 "2026-01-01T00:00:00-05:30",
                 "2026-01-01T00:00:00.123+00:00"):
        assert check(good), f"{good!r} must be accepted"
