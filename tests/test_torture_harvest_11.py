"""Round-11 torture harvest (torture-21) — runtime + gates findings.

Findings (all hermetic, zero Gemini):
  T21-F1 gate unbounded: FIFO/device at EVAL.json (or a hanging plugin
         check) must NOT hang the pipeline — load_eval_json treats
         non-regular files as missing; gate.evaluate runs under
         NINE_GATE_TIMEOUT_S (default 60) and returns BLOCK on expiry.
  T21-F2 cancel during the gate window must durably win (last-line-wins
         JSONLLedger must not get a shipped/blocked line stamped over the
         operator's cancelled line).
  T21-F3 redact() covers github_pat_ / glpat- / Slack webhook URLs /
         AWS STS (ASIA) session keys / lin_api_.
  T21-F4 NINE_MAX_LLM_CALLS <= 0 falls back to the default 24 (a 0/negative
         value would silently DISABLE the ADK budget).
  T21-F5 GET /v1/jobs?status=<typo> -> 422 (CLI parity, t20 F6).
  T21-F6 FirestoreLedger.get/discover tolerate malformed docs (clean
         LedgerError / skip) instead of raw KeyError -> HTTP 500.
"""
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Hermetic env before any app import (server test uses the JSONL ledger).
os.environ["GEMINI_API_KEY"] = ""
os.environ["FIRESTORE_EMULATOR_HOST"] = ""


# ---------------------------------------------------------------- T21-F1 ---
def test_t21_f1_fifo_at_eval_json_is_missing_evidence_not_hang(tmp_path):
    """A FIFO at EVAL.json is NOT evidence — load_eval_json must return
    None immediately (read_text on a FIFO would block forever)."""
    import os as _os

    from nine.gates.evidence import load_eval_json

    _os.mkfifo(tmp_path / "EVAL.json")
    start = time.monotonic()
    assert load_eval_json(tmp_path) is None
    assert time.monotonic() - start < 2.0


def test_t21_f1_fifo_at_verified_json_is_fail_not_hang(tmp_path):
    import os as _os

    from nine.workflows.verify_wf import _honesty_check, _verified_json_check

    _os.mkfifo(tmp_path / "VERIFIED.json")
    (tmp_path / "CHECKS.json").write_text(
        '{"claim_count": 0, "claims": []}', encoding="utf-8")
    start = time.monotonic()
    ok, msg = _verified_json_check({}, tmp_path)
    assert ok is False and "missing" in msg
    assert time.monotonic() - start < 2.0
    # honesty check: CHECKS.json FIFO -> clean gate FAIL, no hang
    (tmp_path / "CHECKS.json").unlink()
    _os.mkfifo(tmp_path / "CHECKS.json")
    start = time.monotonic()
    ok, msg = _honesty_check({}, tmp_path)
    assert ok is False and "not regular files" in msg
    assert time.monotonic() - start < 2.0


def test_t21_f1_gate_hang_times_out_to_block(tmp_path, monkeypatch):
    """A gate check that blocks must produce a BLOCK verdict under
    NINE_GATE_TIMEOUT_S — never a hang."""
    from nine.gates.evidence import EvidenceGate
    from nine.ledger.ledger import JSONLLedger
    from nine.runtime.workflows import WorkflowExecutor

    def _hang(ctx, workdir):
        time.sleep(30)
        return True, "should never finish"

    _hang.expected = []  # type: ignore[attr-defined]
    monkeypatch.setenv("NINE_GATE_TIMEOUT_S", "1")
    gate = EvidenceGate()
    gate.register_check("hang", _hang)
    led = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(led, gate, workdir=tmp_path / "work")
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    verdict = ex._run_gate({"artifact_paths": []}, tmp_path / "work")
    assert time.monotonic() - start < 5.0
    assert verdict["verdict"] == "BLOCK"
    assert "timed out" in verdict["summary"]


def test_t21_f1_gate_fast_path_still_ships(tmp_path):
    """The timeout wrapper must not change the fast-path verdict."""
    from nine.gates.evidence import EvidenceGate
    from nine.ledger.ledger import JSONLLedger
    from nine.runtime.workflows import WorkflowExecutor

    def _ok(ctx, workdir):
        return (workdir / "out.txt").exists(), "ok"

    _ok.expected = ["out.txt"]  # type: ignore[attr-defined]
    gate = EvidenceGate()
    gate.register_check("out", _ok)
    led = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(led, gate, workdir=tmp_path / "work")
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    (tmp_path / "work" / "out.txt").write_text("x", encoding="utf-8")
    verdict = ex._run_gate({"artifact_paths": ["out.txt"]}, tmp_path / "work")
    assert verdict["verdict"] == "SHIP"


# ---------------------------------------------------------------- T21-F2 ---
def test_t21_f2_cancel_during_gate_window_durably_cancelled(tmp_path):
    """An operator cancel landing while the gate is still evaluating must
    durably end the job `cancelled` — the executor's terminal
    shipped/blocked line must never supersede it (append-only
    last-line-wins ledger)."""
    from nine.gates.evidence import EvidenceGate
    from nine.ledger.ledger import JSONLLedger
    from nine.runtime.workflows import Node, Workflow, WorkflowExecutor

    led = JSONLLedger(tmp_path / "ledger.jsonl")
    wf = Workflow(id="gw")
    wf.add_node(Node(id="n", kind="bash",
                     command="echo ok > out.txt"))
    gate = EvidenceGate()

    def _slow_check(ctx, workdir):
        time.sleep(2.5)
        return (workdir / "out.txt").exists(), "ok"

    _slow_check.expected = ["out.txt"]  # type: ignore[attr-defined]
    gate.register_check("out", _slow_check)
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
    time.sleep(1.0)  # node finished; gate check sleeping
    led.cancel(job.job_id)
    th.join(timeout=60)
    assert not th.is_alive(), "executor hung after cancel"

    res = holder["res"]
    assert res["verdict"]["verdict"] == "CANCELLED", res["verdict"]
    rows = [json.loads(line) for line in
            (tmp_path / "ledger.jsonl").read_text().splitlines()
            if line.strip()]
    rows = [r for r in rows if r["job_id"] == job.job_id]
    last = rows[-1]
    assert last["status"] == "cancelled", last["status"]


# ---------------------------------------------------------------- T21-F3 ---
def test_t21_f3_redact_modern_credential_families():
    """github_pat_ / glpat- / Slack webhook / AWS STS (ASIA) / lin_api_
    must be redacted like the older sk-/ghp-/xox families."""
    from nine.router.classifier import redact

    # runtime-split literals: no contiguous secret-shaped bytes in this
    # source file (GitHub push-protection on redaction regression tests).
    gh = "github_pat_" + "11ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"
    gl = "glpat-" + "ABCDEFGHIJKLMNOPQRSTUVWX"
    slack = ("https://hooks.slack.com/services/T00000000/B00000000/"
             + "X" * 20)
    asia = "ASIA" + "1234567890ABCDEF"  # ASIA + 16 (STS session key)
    lin = "lin_api_" + "1234567890abcdef"

    assert redact(gh) == "github_pat_***"
    assert redact(gl) == "glpat-***"
    assert redact(slack) == "https://hooks.slack.com/services/***"
    assert redact(asia) == "ASIA***"
    assert redact(lin) == "lin_api_***"

    # contrast: previously-covered families still redact (no regression)
    assert redact("xoxb-1234567890abcdef") == "xox***"
    assert redact("ghp_1234567890abcdef") == "ghp***"
    assert redact("sk-proj-1234567890abcdef") == "sk***"


# ---------------------------------------------------------------- T21-F4 ---
class _Part:
    def __init__(self, text=None, function_call=None):
        self.text = text
        self.function_call = function_call


class _Content:
    def __init__(self, parts=None):
        self.parts = parts or []


class _Event:
    def __init__(self, is_final_response=False, content=None):
        self.is_final_response = is_final_response
        self.content = content


def _make_node(runner):
    from nine.runtime.adk_runtime import ADKAgentNode

    node = object.__new__(ADKAgentNode)
    node.agent = None
    node.app_name = "nine"
    node.runner = runner
    node._created_sessions = set()
    return node


class _FakeSessionService:
    def __init__(self):
        self.created = []

    async def create_session(self, app_name=None, user_id=None, session_id=None):
        self.created.append((app_name, user_id, session_id))


class _FakeRunner:
    def __init__(self, sequence):
        self._seq = list(sequence)
        self.calls = 0
        self.session_service = _FakeSessionService()

    def run(self, **kwargs):
        self.calls += 1
        item = self._seq.pop(0) if self._seq else []
        if isinstance(item, Exception):
            raise item
        if callable(item):
            return item(**kwargs)
        return item


class _CapturingRunner(_FakeRunner):
    def __init__(self, sequence):
        super().__init__(sequence)
        self.last_run_kwargs = None

    def run(self, **kwargs):
        self.last_run_kwargs = kwargs
        return super().run(**kwargs)


def _one_done_event():
    return _Event(is_final_response=True,
                  content=_Content(parts=[_Part(text="done")]))


@pytest.mark.parametrize("bad", ["0", "-1", "-42"])
def test_t21_f4_max_llm_calls_nonpositive_falls_back(tmp_path, monkeypatch, bad):
    """NINE_MAX_LLM_CALLS=0/-N silently DISABLES the ADK budget ("no
    enforcement ... never ending communication") — clamp to the default."""
    monkeypatch.setenv("NINE_MAX_LLM_CALLS", bad)
    node = _make_node(_CapturingRunner([[_one_done_event()]]))
    node._empty_backoff_s = 0
    node({"task": "hi", "job_id": "j1"}, tmp_path)
    rc = node.runner.last_run_kwargs["run_config"]
    assert rc.max_llm_calls == 24


def test_t21_f4_max_llm_calls_positive_override_kept(tmp_path, monkeypatch):
    monkeypatch.setenv("NINE_MAX_LLM_CALLS", "7")
    node = _make_node(_CapturingRunner([[_one_done_event()]]))
    node._empty_backoff_s = 0
    node({"task": "hi", "job_id": "j1"}, tmp_path)
    assert node.runner.last_run_kwargs["run_config"].max_llm_calls == 7


# ---------------------------------------------------------------- T21-F5 ---
def test_t21_f5_server_rejects_unknown_status_422():
    """A status typo over the API must 422 (CLI parity), not silently
    return an empty ledger."""
    from fastapi.testclient import TestClient

    from deploy.server import app

    client = TestClient(app)
    r = client.get("/v1/jobs?status=shippd")
    assert r.status_code == 422
    assert "unknown status" in r.json()["detail"]
    # valid status still 200
    r2 = client.get("/v1/jobs?status=shipped")
    assert r2.status_code == 200


# ---------------------------------------------------------------- T21-F6 ---
class _FakeDoc:
    def __init__(self, data):
        self._data = data
        self.exists = bool(data)

    def get(self):
        return self

    def to_dict(self):
        return self._data

    def set(self, data, merge=False):
        self._data = {**self._data, **data} if merge else data
        self.exists = True

    def update(self, data):
        self._data.update(data)

    def delete(self):
        self._data = {}


class _FakeStream:
    def __init__(self, docs):
        self._docs = docs

    def __iter__(self):
        return iter(self._docs)


class _FakeCollection:
    def __init__(self):
        self.docs = {}

    def document(self, doc_id):
        if doc_id not in self.docs:
            self.docs[doc_id] = _FakeDoc({})
        return self.docs[doc_id]

    def stream(self):
        return _FakeStream(list(self.docs.values()))


class _FakeFirestore:
    def __init__(self):
        self.collections = {}

    def collection(self, name):
        if name not in self.collections:
            self.collections[name] = _FakeCollection()
        return self.collections[name]


@pytest.fixture
def fake_firestore(monkeypatch):
    fake = _FakeFirestore()
    import google.cloud.firestore as fs

    monkeypatch.setattr(fs, "Client", lambda *a, **kw: fake)
    return fake


def test_t21_f6_firestore_malformed_doc_clean_error_not_keyerror(fake_firestore):
    """A doc missing workflow_id/job_id must raise a clean LedgerError
    (JSONL parity -> 404), never a raw KeyError (-> HTTP 500)."""
    from nine.ledger.firestore_ledger import FirestoreLedger
    from nine.ledger.ledger import LedgerError

    led = FirestoreLedger(collection="nine-jobs")
    led.submit("research", {"task": "a"})
    # corrupt one doc in the store directly
    fake_firestore.collections["nine-jobs"].docs["bad-doc"] = _FakeDoc(
        {"status": "shipped"})
    with pytest.raises(LedgerError):
        led.get("bad-doc")


def test_t21_f6_firestore_discover_skips_malformed_docs(fake_firestore):
    from nine.ledger.firestore_ledger import FirestoreLedger

    led = FirestoreLedger(collection="nine-jobs")
    led.submit("research", {"task": "a"})
    fake_firestore.collections["nine-jobs"].docs["bad-doc"] = _FakeDoc(
        {"status": "shipped"})
    jobs = led.discover()
    ids = [j.job_id for j in jobs]
    assert "bad-doc" not in ids
    assert len(ids) == 1  # the good job survives


# ================================================================ T22 ========
# torture-22 (robustness audit): auxiliary-store writes, timeout-env
# validation, and the CLI OSError belt. All hermetic (no Gemini).

class _BrokenStore:
    """A learner/store whose WRITE side raises OSError (disk broke)."""

    def observe(self, event) -> None:
        raise OSError("events.jsonl is a directory")


def _route_decision():
    from nine.router.classifier import RouteDecision

    return RouteDecision(
        decision_id="d1",
        task_redacted="hello",
        workflow_id="respond",
        confidence=0.5,
        reason="keyword",
        decided_at="2026-08-15T00:00:00Z",
        router_version="test",
    )


def test_t22_f1_route_event_store_record_is_best_effort(tmp_path):
    """events.jsonl replaced by a DIRECTORY mid-run: record() must warn
    and continue (the verdict is already durable in the ledger), never
    raise."""
    from nine.learn.learner import RouteEvent, RouteEventStore

    store = RouteEventStore(tmp_path / "events.jsonl")
    (tmp_path / "events.jsonl").unlink()
    (tmp_path / "events.jsonl").mkdir()
    store.record(RouteEvent(
        event_id="ev-abc12345",
        job_id="j1",
        task_redacted="hello",
        workflow_id="respond",
        confidence=0.5,
        router_version="test",
        verdict="SHIP",
        checks_passed=1,
        checks_total=1,
    ))  # must NOT raise


def test_t22_f1_candidate_store_append_is_best_effort(tmp_path):
    from nine.learn.learner import CandidateStore, ImprovementCandidate

    store = CandidateStore(tmp_path / "cands.jsonl")
    (tmp_path / "cands.jsonl").unlink()
    (tmp_path / "cands.jsonl").mkdir()
    store.append(ImprovementCandidate(
        candidate_id="cand-abcdef12",
        kind="gate",
        description="add a check",
        evidence=["ev-1"],
    ))  # must NOT raise


def test_t22_f1_memory_graph_write_is_best_effort(tmp_path):
    from nine.memory.graph import LocalMemoryGraph

    path = tmp_path / "memory.jsonl"
    path.write_text("", encoding="utf-8")
    path.unlink()
    path.mkdir()  # replace file with a directory
    g = LocalMemoryGraph(path)
    mid = g.save_artifact_summary(
        job_id="j1",
        chain_id="flagship",
        hop_id="build",
        workflow_id="build",
        artifact_name="solution.py",
        kind="code",
        sha256="x" * 64,
        size=10,
        summary="redacted",
        task_redacted="hello",
        verdict="SHIP",
    )
    assert mid  # memory_id returned despite the broken store


def test_t22_f1_registry_save_catalog_is_best_effort(tmp_path, monkeypatch):
    import nine.registry as reg

    monkeypatch.setattr(reg, "_CATALOG_PATH", tmp_path / "catalog")
    (tmp_path / "catalog").write_text("", encoding="utf-8")
    (tmp_path / "catalog").unlink()
    (tmp_path / "catalog").mkdir()
    reg.save_catalog({"keyword_overrides": {}})  # must NOT raise


def test_t22_f1_cli_and_server_route_event_writes_are_best_effort(tmp_path):
    """Both entry-point wrappers swallow a broken LEARN store: the verdict
    is already durable, so a raw traceback / HTTP 500 is never acceptable."""
    import nine.cli as cli_mod
    from deploy.server import _record_route_event as srv_record
    from nine.ledger.ledger import JSONLLedger

    led = JSONLLedger(tmp_path / "ledger.jsonl")
    job = led.submit("respond", {"task": "hello"})
    decision = _route_decision()
    verdict = {"verdict": "SHIP", "eval_results": {}}
    cli_mod._record_route_event(_BrokenStore(), job, decision, verdict)
    srv_record(_BrokenStore(), job, decision, verdict)


def test_t22_f2_timeout_env_zero_fails_before_submit(tmp_path, monkeypatch,
                                                     capsys):
    """NINE_NODE_TIMEOUT_S=0 -> ONE clean error, rc 1, and NO ledger row
    (no 'submitted' zombie)."""
    import argparse

    import nine.cli as cli_mod

    monkeypatch.setenv("NINE_NODE_TIMEOUT_S", "0")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    args = argparse.Namespace(
        task="hello there", ledger=str(tmp_path / "ledger.jsonl"),
        workdir=str(tmp_path / "work"), events=str(tmp_path / "events.jsonl"),
        memory=str(tmp_path / "memory.jsonl"),
    )
    rc = cli_mod.cmd_submit(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "NINE_NODE_TIMEOUT_S" in err
    led_path = tmp_path / "ledger.jsonl"
    rows = led_path.read_text().splitlines() if led_path.exists() else []
    assert rows == []  # nothing durably submitted -> no zombie


@pytest.mark.parametrize("val", ["0", "-5"])
def test_t22_f2_validate_helper_rejects_nonpositive(tmp_path, monkeypatch, val):
    import nine.cli as cli_mod

    monkeypatch.setenv("NINE_NODE_TIMEOUT_S", val)
    with pytest.raises(ValueError, match="0 does NOT mean"):
        cli_mod._validate_node_timeout_env()


@pytest.mark.parametrize("val", ["60", "300", "abc", ""])
def test_t22_f2_validate_helper_accepts_sane(tmp_path, monkeypatch, val):
    import nine.cli as cli_mod

    if val:
        monkeypatch.setenv("NINE_NODE_TIMEOUT_S", val)
    else:
        monkeypatch.delenv("NINE_NODE_TIMEOUT_S", raising=False)
    cli_mod._validate_node_timeout_env()  # must NOT raise


def test_t22_f3_cli_oserror_belt_clean_line_and_durable_failed(
        tmp_path, monkeypatch, capsys):
    """An unreadable artifact (PermissionError from the executor's manifest
    read_bytes) must produce ONE clean line, exit 1, and durably mark the
    job failed — not a raw traceback with a 'running' zombie."""
    import argparse

    import nine.cli as cli_mod
    from nine.ledger.ledger import JSONLLedger

    monkeypatch.setenv("GEMINI_API_KEY", "")
    led = JSONLLedger(tmp_path / "ledger.jsonl")
    job = led.submit("build", {"task": "t"})
    job.transition("routing")
    job.transition("running")
    led.update(job)

    def _boom(self, wf, job, inputs):
        raise PermissionError("artifact is read-only")

    monkeypatch.setattr(cli_mod.WorkflowExecutor, "execute", _boom)
    args = argparse.Namespace(
        workdir=str(tmp_path / "work"), events=str(tmp_path / "events.jsonl"),
        memory=str(tmp_path / "memory.jsonl"),
        ledger=str(tmp_path / "ledger.jsonl"),
    )
    rc = cli_mod._execute_job(led, job, "t", args)
    assert rc == 1
    assert "failed loud" in capsys.readouterr().err
    rows = [json.loads(line) for line in
            (tmp_path / "ledger.jsonl").read_text().splitlines() if line.strip()]
    assert rows[-1]["status"] == "failed"
