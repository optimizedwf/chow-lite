"""Round-12 torture harvest (torture-23 + torture-24) — runtime/gates/CLI.

Findings (all hermetic, zero Gemini):
  T23-F1 (HIGH) recover wipes artifacts AFTER ledger.recover() stamps
         'recovered' -> a PermissionError raw-tracebacked and left a
         durable tombstone (recovered is not recoverable). Wipe FIRST
         (job still blocked/failed), clean OSError -> job stays retryable.
  T23-F2 (MED)  _run_gate daemon thread swallows gate crashes
         (BaseException) -> full NINE_GATE_TIMEOUT_S wasted + BLOCK falsely
         blames "FIFO/device?" + leaked thread. Crash -> immediate BLOCK
         naming the real exception.
  T23-F3 (LOW)  corrupt UTF-8 task.txt raw-tracebacked UnicodeDecodeError
         in cmd_recover; FIFO at task.txt would block the read.
  T24-F1 (MED)  verify lane's check bash node still BLOCKS on a FIFO at
         EVAL.json/CLAIMS.md/claimed refs (exists() is True for FIFOs;
         read_text() hangs 300s) + write-side CHECKS.json/CHECKS.md.
  T24-F2 (MED)  FirestoreLedger shape guard (T21-F6) only checked identity
         fields: wrong-typed created_at/status still raw-TypeError'd
         discover()/stats() -> HTTP 500.
  T24-F3 (MED)  contained_write has NO write-side FIFO guard: a FIFO at
         solution.py/EVAL.json blocks write_text until the node timeout.
  T24-F4 (MED)  flagship review gate _review_verdict_consistent still reads
         EVAL.json via exists() -> FIFO => 60s gate timeout per attempt.
  T24-F5 (LOW)  NINE_MAX_LLM_CALLS non-numeric silently fell back to 24
         (no warning) while the <1 branch warned loudly.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from nine.ledger.ledger import JSONLLedger  # noqa: E402
from nine.runtime.fsafety import contained_write  # noqa: E402
from nine.runtime.workflows import WorkflowExecutor  # noqa: E402

PY = sys.executable


class _SimpleArgs:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _blocked_job_ledger(tmp_path):
    """JSONLLedger with one job parked at 'failed' + task.txt present."""
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    job = ledger.submit("respond", {"task": "hello"})
    job.status = "failed"
    ledger.update(job)
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("hello", encoding="utf-8")
    return ledger, job, job_dir


def _recover_args(tmp_path, job):
    return _SimpleArgs(
        job_id=job.job_id,
        ledger=str(tmp_path / "ledger.jsonl"),
        workdir=str(tmp_path / "work"),
        events=str(tmp_path / "events.jsonl"),
        memory=str(tmp_path / "memory.jsonl"),
    )


# ---------------------------------------------------------------- T23-F1 HIGH


def test_t23_f1_recover_wipe_failure_keeps_job_recoverable(
        tmp_path, monkeypatch, capsys):
    """A PermissionError during the artifact wipe must NOT leave a durable
    'recovered' tombstone: the wipe runs BEFORE ledger.recover(), the job
    stays failed, and a fixed-permission retry succeeds."""
    from nine import cli as nine_cli

    ledger, job, job_dir = _blocked_job_ledger(tmp_path)
    (job_dir / "artifact.md").write_text("stale", encoding="utf-8")

    recover_calls = []

    def _boom_recover(self, job_id):
        recover_calls.append(job_id)
        raise AssertionError("recover() must not run after a failed wipe")

    monkeypatch.setattr(JSONLLedger, "recover", _boom_recover)
    os.chmod(job_dir, 0o500)  # read+exec only -> unlink() PermissionError
    try:
        rc = nine_cli.cmd_recover(_recover_args(tmp_path, job))
    finally:
        os.chmod(job_dir, 0o755)
    assert rc == 1
    assert "failed to clear stale artifacts" in capsys.readouterr().err
    assert recover_calls == []
    # no durable tombstone: still failed, still recoverable
    assert JSONLLedger(tmp_path / "ledger.jsonl").get(job.job_id).status == "failed"


def test_t23_f1_recover_success_path_wipes_before_execute(
        tmp_path, monkeypatch):
    """The wipe completes, then recover()+re-execute run — with the FULL
    task restored (no redacted-task corruption)."""
    from nine import cli as nine_cli

    ledger, job, job_dir = _blocked_job_ledger(tmp_path)
    (job_dir / "artifact.md").write_text("stale", encoding="utf-8")
    executed = {}

    def _fake_execute(ledger_, job_, task_, args_):
        executed["task"] = task_
        return 0

    monkeypatch.setattr(nine_cli, "_execute_job", _fake_execute)
    rc = nine_cli.cmd_recover(_recover_args(tmp_path, job))
    assert rc == 0
    assert executed["task"] == "hello"
    assert not (job_dir / "artifact.md").exists()  # wiped before re-exec
    assert JSONLLedger(tmp_path / "ledger.jsonl").get(job.job_id).status == "recovered"


# ---------------------------------------------------------------- T23-F2 MED


class _CrashGate:
    def __init__(self, exc):
        self.exc = exc

    def evaluate(self, ctx, job_dir):
        raise self.exc


def _executor(tmp_path):
    return WorkflowExecutor(
        JSONLLedger(tmp_path / "ledger.jsonl"), _CrashGate(None),
        workdir=tmp_path / "work")


def test_t23_f2_run_gate_crash_returns_immediate_block(tmp_path, monkeypatch):
    """A gate check raising Exception must BLOCK immediately with the real
    exception named — not burn the full NINE_GATE_TIMEOUT_S."""
    ex = _executor(tmp_path)
    ex.gate = _CrashGate(RuntimeError("boom in gate"))
    monkeypatch.delenv("NINE_GATE_TIMEOUT_S", raising=False)
    t0 = time.monotonic()
    rec = ex._run_gate({"artifact_paths": ["a"]}, tmp_path)
    assert time.monotonic() - t0 < 5
    assert rec["verdict"] == "BLOCK"
    assert "gate crashed: RuntimeError: boom in gate" in rec["summary"]


def test_t23_f2_run_gate_baseexception_crash_block(tmp_path, monkeypatch):
    """SystemExit (BaseException) in a gate check must not die silently in
    the daemon thread and leak the full timeout."""
    ex = _executor(tmp_path)
    ex.gate = _CrashGate(SystemExit(3))
    monkeypatch.delenv("NINE_GATE_TIMEOUT_S", raising=False)
    t0 = time.monotonic()
    rec = ex._run_gate({"artifact_paths": []}, tmp_path)
    assert time.monotonic() - t0 < 5
    assert rec["verdict"] == "BLOCK"
    assert "gate crashed: SystemExit" in rec["summary"]


# ---------------------------------------------------------------- T23-F3 LOW


def test_t23_f3_recover_corrupt_utf8_task_refuses_cleanly(
        tmp_path, monkeypatch, capsys):
    """A non-UTF-8 task.txt must refuse with one clean line, never a raw
    UnicodeDecodeError traceback; the job stays failed."""
    from nine import cli as nine_cli

    ledger, job, job_dir = _blocked_job_ledger(tmp_path)
    (job_dir / "task.txt").write_bytes(b"\xff\xfe\x00task")
    rc = nine_cli.cmd_recover(_recover_args(tmp_path, job))
    assert rc == 1
    assert "not valid UTF-8" in capsys.readouterr().err
    assert JSONLLedger(tmp_path / "ledger.jsonl").get(job.job_id).status == "failed"


def test_t23_f3_recover_fifo_task_txt_no_hang(tmp_path, capsys):
    """A FIFO at task.txt must degrade to the clean 'missing' refusal fast
    (is_file() False), never block the recover read."""
    import os as _os

    from nine import cli as nine_cli

    ledger, job, job_dir = _blocked_job_ledger(tmp_path)
    _os.unlink(job_dir / "task.txt")
    _os.mkfifo(job_dir / "task.txt")
    t0 = time.monotonic()
    rc = nine_cli.cmd_recover(_recover_args(tmp_path, job))
    assert time.monotonic() - t0 < 5
    assert rc == 1
    assert "task.txt is missing" in capsys.readouterr().err


# ---------------------------------------------------------------- T24-F1 MED


def _run_check(tmp_path, script):
    return subprocess.run(
        ["bash", "-c", script.replace("\npython - <<", "\n" + PY + " - <<", 1)],
        cwd=str(tmp_path), capture_output=True, text=True, timeout=40,
        check=False,  # rc is asserted per-test (2 == write-refusal)
    )


def test_t24_f1_check_node_fifo_eval_json_no_hang(tmp_path):
    """CLAIMS.md references EVAL.json; a FIFO at EVAL.json must produce a
    fast FAIL ('missing referenced file(s)') + written CHECKS.json, never a
    300s block."""
    from nine.workflows.verify_wf import _check_command

    (tmp_path / "CLAIMS.md").write_text(
        "1. solution.py implements the task\n"
        "2. `EVAL.json` shows tests pass\n",
        encoding="utf-8")
    (tmp_path / "solution.py").write_text("x = 1\n", encoding="utf-8")
    os.mkfifo(tmp_path / "EVAL.json")
    t0 = time.monotonic()
    r = _run_check(tmp_path, _check_command())
    assert time.monotonic() - t0 < 30
    assert r.returncode == 0, r.stderr
    checks = json.loads((tmp_path / "CHECKS.json").read_text())
    by_n = {c["n"]: c for c in checks["claims"]}
    # claim 2 references EVAL.json -> FIFO counts as missing -> FAIL
    assert by_n[2]["status"] == "FAIL"
    assert "missing referenced file(s)" in by_n[2]["evidence"]
    assert "EVAL.json" in by_n[2]["evidence"]
    # claim 1 references solution.py -> real file -> PASS
    assert by_n[1]["status"] == "PASS"
    assert (tmp_path / "CHECKS.md").exists()


def test_t24_f1_check_node_fifo_write_target_refuses_fast(tmp_path):
    """A pre-existing FIFO at CHECKS.json must refuse loudly (exit 2) fast
    instead of blocking the open() for write."""
    from nine.workflows.verify_wf import _check_command

    (tmp_path / "CLAIMS.md").write_text("1. nothing here\n", encoding="utf-8")
    os.mkfifo(tmp_path / "CHECKS.json")
    t0 = time.monotonic()
    r = _run_check(tmp_path, _check_command())
    assert time.monotonic() - t0 < 30
    assert r.returncode == 2
    assert "refusing to write CHECKS.json" in r.stderr


# ---------------------------------------------------------------- T24-F2 MED


def test_t24_f2_job_from_rec_type_guards():
    from nine.ledger.firestore_ledger import FirestoreLedger

    bad = [
        {"workflow_id": "w", "job_id": "a", "created_at": None},
        {"workflow_id": "w", "job_id": "a", "created_at": 12345},
        {"workflow_id": "w", "job_id": "a", "status": {"x": 1}},
        {"workflow_id": "w", "job_id": "a", "attempts": "three"},
        {"workflow_id": "w", "job_id": "a", "artifacts": {"a": 1}},
        {"workflow_id": "w", "job_id": "a", "verdicts": "nope"},
    ]
    for rec in bad:
        assert FirestoreLedger._job_from_rec(rec) is None, rec
    good = FirestoreLedger._job_from_rec({
        "workflow_id": "w", "job_id": "a",
        "created_at": "2026-08-15T00:00:00+00:00", "status": "blocked",
        "attempts": 2, "artifacts": [], "verdicts": [],
    })
    assert good is not None
    assert good.status == "blocked"


def test_t24_f2_discover_skips_malformed_created_at(monkeypatch):
    """A doc with created_at: null must be skipped, not TypeError the
    sorted() in discover()."""
    from nine.ledger.firestore_ledger import FirestoreLedger
    from tests.test_firestore import FakeDoc, FakeFirestore

    fake = FakeFirestore()
    col = fake.collection("nine-jobs")
    col.docs["healthy"] = FakeDoc({
        "workflow_id": "w", "job_id": "healthy",
        "created_at": "2026-08-15T00:00:00+00:00", "status": "blocked",
    })
    col.docs["bad"] = FakeDoc({
        "workflow_id": "w", "job_id": "bad", "created_at": None,
    })
    led = object.__new__(FirestoreLedger)
    led.db = fake
    led.collection = "nine-jobs"
    led._jobs = {}
    jobs = led.discover()
    assert [j.job_id for j in jobs] == ["healthy"]


def test_t24_f2_stats_buckets_unhashable_status(monkeypatch):
    """A dict-typed status must bucket under '?' — never TypeError in
    stats()."""
    from nine.ledger.firestore_ledger import FirestoreLedger
    from tests.test_firestore import FakeDoc, FakeFirestore

    fake = FakeFirestore()
    col = fake.collection("nine-jobs")
    col.docs["a"] = FakeDoc({
        "workflow_id": "w", "job_id": "a", "status": "blocked"})
    col.docs["b"] = FakeDoc({
        "workflow_id": "w", "job_id": "b", "status": {"x": 1}})
    led = object.__new__(FirestoreLedger)
    led.db = fake
    led.collection = "nine-jobs"
    led._jobs = {}
    st = led.stats()
    assert st["total"] == 2
    assert st["by_status"] == {"blocked": 1, "?": 1}


# ---------------------------------------------------------------- T24-F3 MED


def test_t24_f3_contained_write_refuses_fifo_target(tmp_path):
    """A FIFO at the write target must raise a clean ValueError fast —
    never block write_text until the node timeout."""
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    os.mkfifo(job_dir / "solution.py")
    t0 = time.monotonic()
    with pytest.raises(ValueError, match="non-regular file"):
        contained_write(job_dir, "solution.py", "x = 1")
    assert time.monotonic() - t0 < 5


def test_t24_f3_contained_write_regular_target_still_works(tmp_path):
    job_dir = tmp_path / "job"
    out = contained_write(job_dir, "solution.py", "x = 1")
    assert "wrote solution.py" in out
    assert (job_dir / "solution.py").read_text() == "x = 1"


# ---------------------------------------------------------------- T24-F4 MED


def test_t24_f4_review_gate_fifo_eval_json_instant_fail(tmp_path):
    """A FIFO at EVAL.json must make _review_verdict_consistent return an
    instant False ('missing'), not block for NINE_GATE_TIMEOUT_S."""
    from nine.chains.flagship import _review_verdict_consistent

    (tmp_path / "review.md").write_text(
        "# Verdict: PASS\n", encoding="utf-8")
    os.mkfifo(tmp_path / "EVAL.json")
    t0 = time.monotonic()
    ok, msg = _review_verdict_consistent({}, tmp_path)
    assert time.monotonic() - t0 < 5
    assert ok is False
    assert "missing" in msg


# ---------------------------------------------------------------- T24-F5 LOW


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


def test_t24_f5_non_numeric_max_llm_calls_warns(tmp_path, monkeypatch, capsys):
    """NINE_MAX_LLM_CALLS=abc must print a one-line stderr WARNING (like the
    <1 branch and the junk-env conventions), never fall back silently."""
    from nine.runtime.adk_runtime import ADKAgentNode

    node = object.__new__(ADKAgentNode)
    node.agent = None
    node.app_name = "nine"
    node.runner = _FakeRunner([[]])
    node._created_sessions = set()
    node._attempt_seq = 0
    node._empty_backoff_s = 0
    monkeypatch.setenv("NINE_MAX_LLM_CALLS", "abc")
    with pytest.raises(RuntimeError, match="no output"):
        node({"task": "hi", "job_id": "j1"}, tmp_path)
    err = capsys.readouterr().err
    assert "WARNING: NINE_MAX_LLM_CALLS not an integer" in err
    assert "'abc'" in err
