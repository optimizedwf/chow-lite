"""Slice 34 harvest tests — torture round 8 (torture-15 13 findings +
torture-16 9 findings).

All hermetic (no network, no Gemini): real modules + stubs only.
"""
import argparse
import asyncio
import json
import os
import signal
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace

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


# ============================================================== torture-15 ====
def test_t15_f1_symlink_at_expected_input_is_stale_block(tmp_path):
    """A symlink at a gate-expected input path is a CONTENT CHANGE: the
    gate may follow it and pass, but the file is absent from the shipped
    manifest (symlinks are never evidence) — must BLOCK, never SHIP."""
    outside = tmp_path / "outside.txt"
    outside.write_text("REAL\n", encoding="utf-8")

    wf = Workflow(id="s1")

    def write_fn(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "out.txt").write_text("done\n", encoding="utf-8")
        return {"output": "ok"}

    wf.add_node(Node(id="n", kind="tool", run=write_fn))

    def _reads_input(_ctx, workdir):
        p = Path(workdir) / "input.txt"
        ok = p.read_text(encoding="utf-8").strip() == "REAL"
        return ok, ("input present" if ok else "input wrong")

    # torture-10 F2 tag: the stale guard only audits names a check certifies
    _reads_input.expected = ["input.txt"]  # type: ignore[attr-defined]

    checks = {"input": _reads_input,
              "artifacts": required_artifact_check(["out.txt"])}
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, _gate(checks), workdir=tmp_path / "work")
    job = ledger.submit(wf.id, {"task": "t"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("t\n", encoding="utf-8")
    (job_dir / "input.txt").symlink_to(outside)

    res = ex.execute(wf, job, {"task": "t"}, fix_loop=True)
    assert res["verdict"]["verdict"] == "BLOCK", res["verdict"]["verdict"]
    assert "stale" in res["verdict"]["summary"].lower()
    assert "input.txt" in res["verdict"]["summary"]


def test_t15_f2_unreadable_input_blocks_not_crash(tmp_path):
    """An unreadable gate-expected input is NOT unchanged content: the
    stale guard must BLOCK, never raw-crash on PermissionError."""
    wf = Workflow(id="u1")

    def write_fn(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "out.txt").write_text("done\n", encoding="utf-8")
        return {"output": "ok"}

    wf.add_node(Node(id="n", kind="tool", run=write_fn))
    checks = {"artifacts": required_artifact_check(["required.txt", "out.txt"])}
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, _gate(checks), workdir=tmp_path / "work")
    job = ledger.submit(wf.id, {"task": "t"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("t\n", encoding="utf-8")
    req = job_dir / "required.txt"
    req.write_text("secret\n", encoding="utf-8")
    req.chmod(0o000)
    try:
        res = ex.execute(wf, job, {"task": "t"}, fix_loop=True)
        assert res["verdict"]["verdict"] == "BLOCK", res["verdict"]["verdict"]
        assert "required.txt" in res["verdict"]["summary"]
    finally:
        req.chmod(0o644)


def test_t15_f3_explicit_artifact_path_honors_ignore_lists(tmp_path):
    """The EXPLICIT artifact_path branch must apply the same byproduct
    exclusion as the recursive inventory (a tool naming test_output.log /
    .nine-node-pids / a pyc under .pytest_cache must not re-certify them)."""
    targets = {}

    def log_fn(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "test_output.log").write_text("log\n", encoding="utf-8")
        (jd / "FLAG.txt").write_text("ok\n", encoding="utf-8")
        targets["log"] = str(jd / "test_output.log")
        return {"artifact_path": targets["log"]}

    def pid_fn(inputs, job_dir):
        jd = Path(job_dir)
        sub = jd / "sub"
        sub.mkdir(exist_ok=True)
        (sub / ".nine-node-pids").write_text("1 2\n", encoding="utf-8")
        targets["pid"] = str(sub / ".nine-node-pids")
        return {"artifact_path": targets["pid"]}

    def pyc_fn(inputs, job_dir):
        jd = Path(job_dir)
        cache = jd / "nested" / ".pytest_cache"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "x.pyc").write_bytes(b"\x00\x01")
        targets["pyc"] = str(cache / "x.pyc")
        return {"artifact_path": targets["pyc"]}

    def case_fn(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "OUTPUT.LOG").write_text("case\n", encoding="utf-8")
        targets["case"] = str(jd / "OUTPUT.LOG")
        return {"artifact_path": targets["case"]}

    wf = Workflow(id="ig")
    wf.add_node(Node(id="n1", kind="tool", run=log_fn))
    wf.add_node(Node(id="n2", kind="tool", run=pid_fn))
    wf.add_node(Node(id="n3", kind="tool", run=pyc_fn))
    wf.add_node(Node(id="n4", kind="tool", run=case_fn))
    checks = {"flag": _flag_check,
              "artifacts": required_artifact_check(["FLAG.txt"])}
    res, job, job_dir = _run_workflow(tmp_path, wf, checks)
    assert res["verdict"]["verdict"] == "SHIP", res
    names = [a["name"] for a in job.artifacts]
    assert "test_output.log" not in names
    assert "OUTPUT.LOG" not in names
    assert not any(n.endswith(".pyc") for n in names)
    assert not any(".pytest_cache" in n for n in names)
    assert not any(".nine-node-pids" in n for n in names)


def test_t15_f4_is_ignored_matches_any_part_casefold(tmp_path):
    from nine.runtime.workflows import _is_ignored

    cases = [
        ("sub/.nine-node-pids", True),
        ("a/b/c/OUTPUT.LOG", True),
        ("nested/.pytest_cache/x.pyc", True),
        ("__pycache__/m.pyc", True),
        ("out.txt", False),
        ("sub/notes.md", False),
    ]
    for rel, want in cases:
        assert _is_ignored(rel, tmp_path / rel) is want, rel


def test_t15_f5_outside_artifact_namespaced_no_collision(tmp_path):
    """An artifact outside the job dir registers as '../<name>' — it can
    never collide with (or silently replace) a same-named inside file."""
    outside = tmp_path / "outside.txt"
    outside.write_text("outside content\n", encoding="utf-8")

    def write_fn(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "FLAG.txt").write_text("ok\n", encoding="utf-8")
        (jd / "outside.txt").write_text("inside content\n", encoding="utf-8")
        return {"artifact_path": str(outside)}

    wf = Workflow(id="out")
    wf.add_node(Node(id="n", kind="tool", run=write_fn))
    checks = {"flag": _flag_check,
              "artifacts": required_artifact_check(["FLAG.txt"])}
    res, job, job_dir = _run_workflow(tmp_path, wf, checks)
    assert res["verdict"]["verdict"] == "SHIP", res
    names = [a["name"] for a in job.artifacts]
    assert "../outside.txt" in names, names
    assert "outside.txt" in names, names  # inside file still present
    inside = [a for a in job.artifacts if a["name"] == "outside.txt"][0]
    assert inside["size"] == len("inside content\n")


def test_t15_f6_redact_uri_userinfo_password_flag_basic_auth():
    from nine.router.classifier import redact

    cases = [
        ("mongodb://alice:hunter2@db.example.com:27017/app", "hunter2"),
        ("run --password hunter2 now", "hunter2"),
        ("Authorization: Basic dXNlcjpwYXNz", "dXNlcjpwYXNz"),
        ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
    ]
    for text, secret in cases:
        out = redact(text)
        assert secret not in out, f"leaked {secret!r} from {text!r} -> {out!r}"
    # the URI host is preserved (not over-redacted)
    out = redact("mongodb://alice:hunter2@db.example.com:27017/app")
    assert "db.example.com" in out


def test_t15_f7_outside_artifact_summary_from_own_content(tmp_path):
    """An outside-job-dir artifact's memory summary must quote ITS OWN
    content, never the job dir's HANDOFF.md (which used to be read for
    every artifact)."""
    from nine.memory.graph import LocalMemoryGraph

    outside = tmp_path / "outside-artifact.md"
    outside.write_text("OUTSIDE OWN CONTENT 12345\n", encoding="utf-8")

    def hop1_run(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "HANDOFF.md").write_text(
            "HANDOFF PLAN SECRET 99999\n", encoding="utf-8")
        (jd / "FLAG.txt").write_text("ok\n", encoding="utf-8")
        return {"artifact_path": str(outside)}

    h1 = Workflow(id="h1")
    h1.add_node(Node(id="n", kind="tool", run=hop1_run))
    chain = Chain(id="c", hops=[
        Hop("h1", h1, ["FLAG.txt"],
            {"flag": _flag_check,
             "artifacts": required_artifact_check(["FLAG.txt"])}, 1),
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
    outside_recs = [r for r in recs
                    if r.get("artifact_name") == "../outside-artifact.md"]
    assert outside_recs, [r.get("artifact_name") for r in recs]
    for r in outside_recs:
        assert "OUTSIDE OWN CONTENT" in r["summary"], r
        assert "HANDOFF PLAN" not in r["summary"], r


def test_t15_f8_non_routable_normalize_apply_and_keywords(tmp_path, monkeypatch,
                                                         capsys):
    """NON_ROUTABLE refusal is case/whitespace-insensitive on BOTH sides
    (_apply_candidate wf_id and _merged_keywords catalog entries)."""
    import nine.registry as reg
    from nine import cli as nine_cli
    from nine.learn.learner import ImprovementCandidate, Learner, RouteEventStore

    bad = tmp_path / "catalog.json"
    bad.write_text(json.dumps({
        "keyword_overrides": {" Inbox-Triage-Task-Report ": ["foo"]},
    }), encoding="utf-8")
    monkeypatch.setattr(reg, "_CATALOG_PATH", bad)
    kw = reg._merged_keywords()
    assert " Inbox-Triage-Task-Report " not in kw

    lr = Learner(RouteEventStore(tmp_path / "events.jsonl"))
    cand = ImprovementCandidate(
        candidate_id="c1", kind="keyword", description="d", evidence=[],
        status="pending",
        params={"workflow_id": " Inbox-Triage-Task-Report ", "keyword": "zzz"},
    )
    lr.cands.append(cand)
    rc = nine_cli._apply_candidate(lr, "c1")
    out = capsys.readouterr()
    assert rc == 1
    assert "demo lane" in out.err or "non-routable" in out.err.casefold()
    assert lr.cands.get("c1").status == "pending"


def test_t15_f9_kill_node_groups_identity_gate(tmp_path):
    """The external bench killer only kills a pid that is STILL the session
    leader AND whose spawn time matches the recorded epoch — a recycled or
    unverifiable pid is skipped conservatively."""
    from bench import bench_nine as bm

    job_dir = tmp_path / "work" / "j1"
    job_dir.mkdir(parents=True)
    pid_file = job_dir / ".nine-node-pids"

    # (a) nonexistent pid: skipped, no crash
    pid_file.write_text("999999999 1000000000\n", encoding="utf-8")
    assert bm._kill_node_groups(tmp_path / "work") == 0

    proc = subprocess.Popen(["bash", "-c", "sleep 60"], start_new_session=True)
    try:
        # (b) live session leader with WRONG epoch: recycled-pid guard skips
        pid_file.write_text(f"{proc.pid} 1000000000\n", encoding="utf-8")
        assert bm._kill_node_groups(tmp_path / "work") == 0
        assert proc.poll() is None, "wrong-epoch pid must NOT be killed"

        # (c) matching epoch: killed
        real_epoch = bm._node_start_epoch(proc.pid)
        assert real_epoch is not None
        pid_file.write_text(f"{proc.pid} {real_epoch:.3f}\n", encoding="utf-8")
        killed = bm._kill_node_groups(tmp_path / "work")
        assert killed >= 1
        proc.wait(timeout=5)
        assert proc.poll() is not None
    finally:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            pass


def test_t15_f10_convert_inlines_per_call_site_constants(tmp_path):
    """A constant REASSIGNED between test calls is inlined per call site —
    inlining the last assignment everywhere would assert the wrong contract."""
    from bench.bench_nine import convert_to_pytest

    runner = (
        "from implementation import add\n"
        "EXPECTED = 5\n"
        'test("a", lambda: add(2, 3), EXPECTED)\n'
        "EXPECTED = 6\n"
        'test("b", lambda: add(2, 3), EXPECTED)\n'
    )
    src = convert_to_pytest(runner)
    assert "== 5" in src, src
    assert "== 6" in src, src
    assert "EXPECTED" not in src, src  # fully inlined, nothing dangling


def test_t15_f11_convert_slugs_non_literal_name_arg(tmp_path):
    """A non-literal first arg (test_raises(EXC, ...)) must not raw-crash
    ast.literal_eval — the call-site name becomes a readable slug."""
    from bench.bench_nine import convert_to_pytest

    runner = (
        "from implementation import add\n"
        "EXC = ValueError\n"
        'test_raises(EXC, lambda: add(2, 3))\n'
    )
    src = convert_to_pytest(runner)
    assert "def test_01_exc" in src, src
    assert "pytest.raises" in src, src


def test_t15_f12_learn_memory_stores_clean_502(tmp_path, monkeypatch):
    """A bad NINE_DATA_DIR must surface LedgerUnavailable (clean 502) from
    the LEARN and MEMORY stores, not a raw 500."""
    import deploy.server as server

    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file\n", encoding="utf-8")
    monkeypatch.setattr(server, "EVENTS_PATH", blocker / "events.jsonl")
    with pytest.raises(server.LedgerUnavailable):
        server.get_learner()
    monkeypatch.setattr(server, "MEMORY_PATH", blocker / "memory.jsonl")
    with pytest.raises(server.LedgerUnavailable):
        server.get_memory()


def test_t15_f13_fallback_latches_after_engage(tmp_path, monkeypatch):
    """Once the JSONL fallback ENGAGES, get_ledger() returns the plain JSONL
    ledger — the Firestore round trip is never retried per request."""
    import deploy.server as server
    from nine.ledger.ledger import JSONLLedger

    monkeypatch.setattr(server, "LEDGER_PATH", tmp_path / "jobs" / "ledger.jsonl")
    monkeypatch.setattr(server, "_ledger", None)
    monkeypatch.setattr(server, "_ledger_failed", False)

    calls = {"submit": 0}

    class FakeFirestore:
        def __init__(self, collection="x"):
            pass

        def submit(self, *a, **k):
            calls["submit"] += 1
            raise RuntimeError("firestore down")

    monkeypatch.setattr(server, "FirestoreLedger", FakeFirestore)

    proxy = server.get_ledger()
    assert isinstance(proxy, server._LazyFallbackLedger)
    proxy.submit("wf", {"task": "t"})  # falls back to JSONL
    assert calls["submit"] == 1
    assert server._ledger_failed is True

    led2 = server.get_ledger()
    assert not isinstance(led2, server._LazyFallbackLedger)
    assert isinstance(led2, JSONLLedger)
    led2.submit("wf2", {"task": "t2"})
    assert calls["submit"] == 1, "Firestore must never be retried after latch"


# ============================================================== torture-16 ====
def test_t16_f1_cancelled_verdict_durable_and_route_event_skipped(tmp_path):
    """An operator-cancel mid-run yields a durable CANCELLED verdict (which
    the evidence-verdict schema admits) and NO route event is recorded."""
    from nine import cli as nine_cli
    from nine.learn.learner import Learner, RouteEventStore

    wf = Workflow(id="cx")
    wf.add_node(Node(id="n", kind="bash",
                     command="echo ok > out.txt && sleep 8"))
    checks = {"artifacts": required_artifact_check(["out.txt"])}
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, _gate(checks), workdir=tmp_path / "work")
    job = ledger.submit(wf.id, {"task": "t"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("t\n", encoding="utf-8")

    holder: dict = {}

    def _run():
        holder["res"] = ex.execute(wf, job, {"task": "t"}, fix_loop=True)

    th = threading.Thread(target=_run)
    th.start()
    time.sleep(1.0)  # let the bash node start
    ledger.cancel(job.job_id)
    th.join(timeout=60)
    assert not th.is_alive(), "cancel did not stop the run"
    res = holder["res"]
    assert res["verdict"]["verdict"] == "CANCELLED", res["verdict"]["verdict"]

    rows = [json.loads(line) for line in
            (tmp_path / "ledger.jsonl").read_text().splitlines() if line.strip()]
    last = rows[-1]
    # the operator's terminal line is the durable cancel; nothing is stamped
    # over it (no shipped/blocked row may follow a cancel)
    assert last["status"] == "cancelled", last
    # the run's own job carries the validated CANCELLED verdict
    # (job.add_verdict — the schema admits CANCELLED w/ gate_version null)
    assert any(v.get("verdict") == "CANCELLED" for v in job.verdicts), job.verdicts

    # route event: skipped, nothing recorded, no exception
    lr = Learner(RouteEventStore(tmp_path / "events.jsonl"))
    nine_cli._record_route_event(
        lr, job,
        SimpleNamespace(workflow_id="cx", task_redacted="t"),
        res["verdict"],
    )
    assert (tmp_path / "events.jsonl").read_text().strip() == ""


def test_t16_f2_date_time_format_is_enforced(tmp_path):
    """`format: date-time` is a REAL constraint now (FormatChecker) —
    garbage timestamps fail validate() at every boundary."""
    from nine.schema_validation import SchemaValidationError, validate

    good = {
        "decision_id": "d1", "task_redacted": "t", "workflow_id": "respond",
        "confidence": 0.9, "reason": "r",
        "decided_at": "2026-01-01T00:00:00+00:00", "router_version": "0.1.0",
    }
    validate("route-decision", good)
    bad = dict(good)
    bad["decided_at"] = "now"
    with pytest.raises(SchemaValidationError):
        validate("route-decision", bad)

    v = {
        "verdict": "SHIP", "evidence_refs": ["a"], "eval_results": {},
        "summary": "s", "verified_at": "not-a-date", "gate_version": "0.1.0",
    }
    with pytest.raises(SchemaValidationError):
        validate("evidence-verdict", v)


def test_t16_f3_mutators_validate_boundary_objects(tmp_path):
    """Boundary objects are validated AT THE MUTATORS — malformed route
    decisions / artifacts / verdicts are rejected, never written."""
    from nine.router.classifier import RouteDecision
    from nine.schema_validation import SchemaValidationError

    led = JSONLLedger(tmp_path / "ledger.jsonl")
    job = led.submit("respond", {"task": "t"})

    dec = RouteDecision(
        decision_id="d1", task_redacted="t", workflow_id="respond",
        confidence=0.9, reason="r",
        decided_at="2026-01-01T00:00:00+00:00", router_version="v",
    )
    job.attach_route_decision(dec)
    with pytest.raises(SchemaValidationError):
        job.attach_route_decision(RouteDecision(
            decision_id="d2", task_redacted="t", workflow_id="respond",
            confidence=0.9, reason="r", decided_at=None, router_version="v"))
    with pytest.raises(SchemaValidationError):
        job.add_artifact({"name": "x"})  # missing path/kind/sha256/size/...
    with pytest.raises(SchemaValidationError):
        job.add_verdict({
            "verdict": "BOGUS", "evidence_refs": [],
            "verified_at": "2026-01-01T00:00:00+00:00",
        })


def test_t16_f4_redact_word_boundary_and_quoted_tails():
    from nine.router.classifier import redact

    # word boundary: no over-redaction of sk/pk substrings inside words
    out = redact("the skillfulness of the model exposes nothing")
    assert "skillfulness" in out
    assert "exposes" in out
    # comparison tails: == hunter3 (whole tail), not just 'hunter'
    out = redact("password == hunter3 and other stuff")
    assert "hunter3" not in out
    # quoted token with trailing junk inside the quoted string
    out = redact('"token": "sk-123 abc"')
    assert "sk-123" not in out
    # pk-live + ghp_ + sk-proj shapes
    for text, secret in [
        ("key: pk-live-abcdefghijklmnopqrstuvwxyz0123456789",
         "pk-live-abcdefghijklmnopqrstuvwxyz0123456789"),
        ("token = ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij",
         "ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"),
        ("sk-proj-abc123def456", "sk-proj-abc123def456"),
    ]:
        assert secret not in redact(text), secret


def test_t16_f5_deploy_refuses_public_without_api_key():
    """deploy.sh must never deploy a public unauthenticated API without a
    NINE_API_KEY — it wires the secret or falls back to private deploy."""
    sh = (Path(__file__).resolve().parent.parent / "deploy" / "deploy.sh")
    text = sh.read_text(encoding="utf-8")
    assert "--no-allow-unauthenticated" in text
    assert "refusing to deploy a PUBLIC unauthenticated API" in text
    assert "--set-secrets NINE_API_KEY=" in text
    assert "gcloud secrets create NINE_API_KEY" in text


def test_t16_f6_rate_limiter_evicts_idle_and_auth_first(tmp_path, monkeypatch):
    """Rate-limiter table tracks ACTIVE IPs only (idle entries evicted), and
    auth is checked BEFORE rate so wrong keys never consume the shared
    per-IP quota."""
    import deploy.server as server

    # idle eviction: a stale deque entry is removed by the sweep
    ip = "10.0.0.99"
    old = time.monotonic() - server.RATE_LIMIT["window_s"] - 5
    server._hits[ip] = deque([old, old - 1])
    server._hits_swept = time.monotonic() - server.RATE_LIMIT["window_s"] - 1
    req = SimpleNamespace(client=SimpleNamespace(host=ip))
    server._check_rate_limit(req)
    assert len(server._hits[ip]) == 1
    assert server._hits[ip][0] > old  # fresh entry, not the stale one

    # auth BEFORE rate: a bad key is rejected without touching the limiter
    calls = {"rate": 0}

    async def _boom(request):
        calls["rate"] += 1
        raise AssertionError("rate limit must not run for bad keys")

    async def _next(request):
        return "passed"

    monkeypatch.setattr(server, "_check_rate_limit", _boom)
    monkeypatch.setattr(server, "_API_KEY", "sekret")
    denied = asyncio.run(server._guard(
        SimpleNamespace(headers={"x-api-key": "wrong",
                                 "content-length": "10"},
                        url=SimpleNamespace(path="/v1/events")),
        _next))
    assert denied.status_code == 401
    assert calls["rate"] == 0


def test_t16_f7_events_file_parent_clean_error(tmp_path):
    """A bad --events path (parent component is a FILE) yields ONE clean
    error line from the shared submit/recover path — never a traceback."""
    from nine import cli as nine_cli

    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file\n", encoding="utf-8")
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    job = ledger.submit("respond", {"task": "t"})
    args = argparse.Namespace(events=str(blocker / "events.jsonl"))
    rc = nine_cli._execute_job(ledger, job, "t", args)
    assert rc == 1


def test_t16_f8_docs_claim_gemini_36_flash():
    """Docs truth: the runtime model is gemini-3.6-flash; no stale
    'Gemini 3.5 Flash' claims, and test counts reflect the real suite."""
    base = Path(__file__).resolve().parent.parent
    for name in ("README.md", "SUBMISSION.md"):
        p = base / name
        if not p.exists():
            continue
        text = p.read_text(encoding="utf-8")
        assert "Gemini 3.5 Flash" not in text, name
        assert "3.6" in text, name


def test_t16_f9_commit_failure_loud_and_candidate_untouched(
        tmp_path, monkeypatch, capsys):
    """When _git_commit fails the catalog change stays on disk but the
    candidate is NOT marked applied — one loud warning, clean exit."""
    import nine.registry as reg
    from nine import cli as nine_cli
    from nine.learn.learner import ImprovementCandidate, Learner, RouteEventStore

    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"keyword_overrides": {}}), encoding="utf-8")
    monkeypatch.setattr(reg, "_CATALOG_PATH", catalog)
    monkeypatch.setattr(nine_cli, "_regression_green", lambda: True)
    monkeypatch.setattr(nine_cli, "_git_commit", lambda msg: False)

    lr = Learner(RouteEventStore(tmp_path / "events.jsonl"))
    cand = ImprovementCandidate(
        candidate_id="c1", kind="keyword", description="d", evidence=[],
        status="pending",
        params={"workflow_id": "research", "keyword": "researchfy"},
    )
    lr.cands.append(cand)
    rc = nine_cli._apply_candidate(lr, "c1")
    assert rc == 1
    assert lr.cands.get("c1").status == "pending",         "candidate must NOT be marked applied on a failed commit"
    data = json.loads(catalog.read_text(encoding="utf-8"))
    assert "researchfy" in data["keyword_overrides"]["research"]
