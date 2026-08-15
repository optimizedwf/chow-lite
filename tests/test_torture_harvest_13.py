"""Round-13 torture harvest (torture-25 + torture-26) — clean-error and
junk-env contracts, CLI + gate + engine.

Findings (all hermetic, zero Gemini):
  T25-F1 (MED)  cmd_submit and cmd_chain raw-traceback LedgerError when the
         ledger is un-appendable (chmod 444 / full disk) — the submit line
         sat OUTSIDE every try. Both must print ONE clean "cannot submit
         to ledger: ..." line and return 1.
  T25-F2 (LOW)  cmd_recover caught only UnicodeDecodeError on task.txt;
         a chmod-000 task.txt passes is_file() and read_text raises
         PermissionError (an OSError) -> raw traceback. Now one clean
         "task.txt is not readable" line.
  T25-F3 (LOW)  debug_wf's NINE_TASK_CAP / NINE_INSTRUCTION_LIMIT int()
         parses raw-traceback on junk ("2k", "-1", "0") -> warn + fallback
         to the 1400 default, same convention as NINE_MAX_LLM_CALLS.
  T25-F4 (LOW)  NINE_LLM_TIMEOUT_S="nan"/-5/1e400 parse fine and kill every
         requests.post with a library-level error naming no env var.
         Now: non-finite or < 1 -> warn + fallback 120.0.
  T26-F1 (MED)  stale guard re-reads seeded inputs (test_solution.py,
         task.txt) via Path.read_bytes() on the MAIN thread — a FIFO
         swapped in after the attempt-1 snapshot hangs forever
         (cancel-proof, outside every timeout). Reads now go through
         _try_read_bytes (O_NONBLOCK + S_ISREG guard) -> BLOCK, never hang.
  T26-F2 (LOW)  file_nonempty_check certified a DIRECTORY at RESPONSE.md
         as a non-trivial answer (exists + stat().st_size >= min_chars).
         Now requires a regular file; required_artifact_check still
         accepts directories (build-multi solution/) but rejects
         FIFOs/devices.
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("NINE_TASK_CAP", "NINE_INSTRUCTION_LIMIT", "NINE_LLM_TIMEOUT_S",
              "NINE_MAX_LLM_CALLS", "NINE_GATE_TIMEOUT_S"):
        monkeypatch.delenv(k, raising=False)


# ---------------------------------------------------------------------------
# T25-F1: ledger.submit LedgerError -> one clean line (submit + chain)
# ---------------------------------------------------------------------------
class _UnappendableLedger:
    """Minimal stand-in whose submit raises LedgerError (as a real
    un-appendable ledger file would on open/append)."""
    def __init__(self):
        from nine.ledger.ledger import LedgerError
        self._exc = LedgerError("permission denied: ledger.jsonl is read-only")

    def submit(self, *a, **k):
        raise self._exc

    def update(self, job):
        raise self._exc

    def refresh(self, *a, **k):
        raise self._exc

    def recover(self, *a, **k):
        raise self._exc


def test_submit_unappendable_ledger_clean_error(tmp_path, monkeypatch):
    """A chmod-444 (or otherwise un-appendable) ledger must NOT raw-traceback
    LedgerError from cmd_submit — one clean stderr line, exit 1."""
    from nine.cli import main

    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("", encoding="utf-8")
    ledger.chmod(0o444)
    monkeypatch.setattr("nine.ledger.ledger.JSONLLedger", lambda *a, **k: _UnappendableLedger())
    r = main(["--ledger", str(ledger), "submit", "research the printing press"])
    assert r == 1
    ledger.chmod(0o644)


def test_chain_unappendable_ledger_clean_error(tmp_path, monkeypatch):
    from nine.cli import main

    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("", encoding="utf-8")
    ledger.chmod(0o444)
    monkeypatch.setattr("nine.ledger.ledger.JSONLLedger", lambda *a, **k: _UnappendableLedger())
    r = main(["--ledger", str(ledger), "chain", "flagship", "build a calculator"])
    assert r == 1
    ledger.chmod(0o644)


# ---------------------------------------------------------------------------
# T25-F2: cmd_recover chmod-000 task.txt -> clean error (OSError caught)
# ---------------------------------------------------------------------------
class _FakeLedger:
    def __init__(self, job, base):
        self._job = job
        self._base = base

    def get(self, job_id):
        return self._job

    def submit(self, *a, **k):
        return self._job

    def update(self, *a, **k):
        pass

    def refresh(self, job_id):
        return self._job

    def recover(self, *a, **k):
        return self._job


def test_recover_unreadable_task_txt_clean_error(tmp_path, monkeypatch):
    """A chmod-000 task.txt passes is_file() but read_text raises
    PermissionError. recover must print one clean line and return 1, not a
    raw traceback."""
    from nine.cli import main
    from nine.ledger.ledger import Job

    ledger = tmp_path / "ledger.jsonl"
    ledger.write_text("", encoding="utf-8")
    job = Job("respond", {"task": "t"}, job_id="j1")
    job.transition("routing")
    job.transition("blocked")
    monkeypatch.setattr("nine.ledger.ledger.JSONLLedger", lambda *a, **k: _FakeLedger(job, tmp_path))
    job_dir = tmp_path / "work" / "j1"
    job_dir.mkdir(parents=True)
    task_txt = job_dir / "task.txt"
    task_txt.write_text("do the thing", encoding="utf-8")
    task_txt.chmod(0o000)
    r = main(["--ledger", str(ledger), "recover", "j1"])
    assert r == 1
    task_txt.chmod(0o644)


# ---------------------------------------------------------------------------
# T25-F3: debug_wf junk env knobs -> warn + fallback
# ---------------------------------------------------------------------------
def test_debug_wf_junk_task_cap_warns_and_falls_back(monkeypatch, capsys):
    from nine.workflows import debug_wf

    monkeypatch.setenv("NINE_TASK_CAP", "2k")
    assert debug_wf._env_cap("NINE_TASK_CAP") == 1400
    assert "NINE_TASK_CAP" in capsys.readouterr().err


def test_debug_wf_nonpositive_task_cap_warns_and_falls_back(monkeypatch, capsys):
    from nine.workflows import debug_wf

    monkeypatch.setenv("NINE_TASK_CAP", "0")
    assert debug_wf._env_cap("NINE_TASK_CAP") == 1400
    assert "NINE_TASK_CAP" in capsys.readouterr().err


def test_debug_wf_valid_task_cap_used(monkeypatch, capsys):
    from nine.workflows import debug_wf

    monkeypatch.setenv("NINE_TASK_CAP", "700")
    assert debug_wf._env_cap("NINE_TASK_CAP") == 700
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# T25-F4: NINE_LLM_TIMEOUT_S non-finite / <1 -> warn + fallback 120.0
# ---------------------------------------------------------------------------
def test_llm_timeout_nan_falls_back(monkeypatch, capsys):
    from nine.runtime.llm_provider import _tunnel_timeout

    monkeypatch.setenv("NINE_LLM_TIMEOUT_S", "nan")
    assert _tunnel_timeout() == 120.0
    assert "NINE_LLM_TIMEOUT_S" in capsys.readouterr().err


def test_llm_timeout_negative_falls_back(monkeypatch, capsys):
    from nine.runtime.llm_provider import _tunnel_timeout

    monkeypatch.setenv("NINE_LLM_TIMEOUT_S", "-5")
    assert _tunnel_timeout() == 120.0
    assert "NINE_LLM_TIMEOUT_S" in capsys.readouterr().err


def test_llm_timeout_huge_falls_back(monkeypatch, capsys):
    from nine.runtime.llm_provider import _tunnel_timeout

    monkeypatch.setenv("NINE_LLM_TIMEOUT_S", "1e400")
    assert _tunnel_timeout() == 120.0
    assert "NINE_LLM_TIMEOUT_S" in capsys.readouterr().err


def test_llm_timeout_junk_falls_back(monkeypatch, capsys):
    """A non-numeric value hits the ValueError path — silent fallback 120."""
    from nine.runtime.llm_provider import _tunnel_timeout

    monkeypatch.setenv("NINE_LLM_TIMEOUT_S", "fast")
    assert _tunnel_timeout() == 120.0


def test_llm_timeout_valid_used(monkeypatch, capsys):
    from nine.runtime.llm_provider import _tunnel_timeout

    monkeypatch.setenv("NINE_LLM_TIMEOUT_S", "300")
    assert _tunnel_timeout() == 300.0
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# T26-F1: stale-guard input re-read must never hang on a FIFO
# ---------------------------------------------------------------------------
def test_try_read_bytes_fifo_returns_none(tmp_path):
    """A FIFO at a seeded-input path must read as None immediately (no
    main-thread hang), not block forever."""
    from nine.runtime.workflows import WorkflowExecutor

    fifo = tmp_path / "test_solution.py"
    os.mkfifo(fifo)
    data = WorkflowExecutor._try_read_bytes(fifo)
    assert data is None


def test_try_read_bytes_regular_file_returns_content(tmp_path):
    from nine.runtime.workflows import WorkflowExecutor

    f = tmp_path / "task.txt"
    f.write_text("do the thing", encoding="utf-8")
    assert WorkflowExecutor._try_read_bytes(f) == b"do the thing"


def test_try_read_bytes_directory_returns_none(tmp_path):
    from nine.runtime.workflows import WorkflowExecutor

    d = tmp_path / "solution"
    d.mkdir()
    assert WorkflowExecutor._try_read_bytes(d) is None


def test_try_read_bytes_missing_returns_none(tmp_path):
    from nine.runtime.workflows import WorkflowExecutor

    assert WorkflowExecutor._try_read_bytes(tmp_path / "nope") is None


# ---------------------------------------------------------------------------
# T26-F2: gate checks reject dirs/FIFOs as artifacts
# ---------------------------------------------------------------------------
def test_file_nonempty_rejects_directory(tmp_path):
    from nine.gates.evidence import file_nonempty_check

    d = tmp_path / "RESPONSE.md"
    d.mkdir()
    ok, msg = file_nonempty_check("RESPONSE.md", min_chars=10)({}, tmp_path)
    assert not ok and "not a regular file" in msg


def test_file_nonempty_accepts_real_file(tmp_path):
    from nine.gates.evidence import file_nonempty_check

    (tmp_path / "RESPONSE.md").write_text("a real answer with enough chars", encoding="utf-8")
    ok, _ = file_nonempty_check("RESPONSE.md", min_chars=10)({}, tmp_path)
    assert ok


def test_required_artifact_rejects_fifo(tmp_path):
    from nine.gates.evidence import required_artifact_check

    fifo = tmp_path / "EVAL.json"
    os.mkfifo(fifo)
    ok, msg = required_artifact_check(["EVAL.json"])({}, tmp_path)
    assert not ok and "missing artifacts" in msg


def test_required_artifact_still_accepts_directory(tmp_path):
    """build-multi certifies the solution/ DIRECTORY artifact — keep that
    legal while rejecting non-file non-dir (FIFO/device) entries."""
    from nine.gates.evidence import required_artifact_check

    (tmp_path / "solution").mkdir()
    ok, _ = required_artifact_check(["solution"])({}, tmp_path)
    assert ok
