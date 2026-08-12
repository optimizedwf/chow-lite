"""Regression tests for the bench-nine findings (2026-08-12).

Covers the highest-value gaps found by the benchmark run:
1. ADK empty streams (Gemini 429 quota exhaustion) must FAIL LOUD, never
   pass silently and SHIP an unmodified artifact (gaps 1+6).
2. build self-test must run pytest when test_solution.py exists — a buggy
   solution.py that exits 0 must NOT ship (gap 2).
3. debug must SHIP a perfect patch even when ROOT_CAUSE.md is missing
   (gap 3).
4. pytest collection errors must report a clear message, not
   "0 test(s) failed, 0 passed" (gap 7).
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

os.environ["GEMINI_API_KEY"] = ""

from nine.gates.evidence import EvidenceGate
from nine.ledger.ledger import JSONLLedger
from nine.runtime.workflows import Node, WorkflowExecutor


def _gate(hop):
    g = EvidenceGate()
    for name, check in hop.gate_checks.items():
        g.register_check(name, check)
    return g


def _execute(hop, tmp_path, inputs=None, seed=None):
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate=_gate(hop), workdir=tmp_path / "work")
    job = ledger.submit(hop.id, {"task": inputs or "fix the bug"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    if seed:
        for name, content in seed.items():
            (job_dir / name).write_text(content, encoding="utf-8")
    res = ex.execute(hop.workflow, job, {"task": inputs or "fix the bug"})
    return res, job, job_dir


# ---------------------------------------------------------------- gap 1+6
def test_adk_empty_stream_raises_not_silent_pass(tmp_path):
    """An empty agent stream (no text, no tool calls) must raise."""
    from types import SimpleNamespace

    from nine.runtime.adk_runtime import ADKAgentNode

    async def _create_session(**kw):
        return None

    def _node(events):
        n = object.__new__(ADKAgentNode)
        n.agent = None
        n.app_name = "nine"
        n.runner = SimpleNamespace(
            run=lambda **kw: iter(events),
            session_service=SimpleNamespace(create_session=_create_session),
        )
        n._created_sessions = set()
        return n

    # fully empty stream
    with pytest.raises(RuntimeError, match="produced no output"):
        _node([])({"task": "fix"}, tmp_path)
    # non-empty stream but no text and no tool calls
    ev = SimpleNamespace(
        content=SimpleNamespace(parts=[SimpleNamespace(function_call=None, text=None)]),
        is_final_response=True,
    )
    with pytest.raises(RuntimeError, match="produced no output"):
        _node([ev])({"task": "fix"}, tmp_path)


def test_adk_stream_with_text_succeeds(tmp_path):
    from types import SimpleNamespace

    from nine.runtime.adk_runtime import ADKAgentNode

    async def _create_session(**kw):
        return None

    n = object.__new__(ADKAgentNode)
    n.agent = None
    n.app_name = "nine"
    ev = SimpleNamespace(
        content=SimpleNamespace(parts=[SimpleNamespace(function_call=None, text="hello world")]),
        is_final_response=True,
    )
    n.runner = SimpleNamespace(
        run=lambda **kw: iter([ev]),
        session_service=SimpleNamespace(create_session=_create_session),
    )
    n._created_sessions = set()
    out = n({"task": "hi"}, tmp_path)
    assert out["output"] == "hello world"
    assert (tmp_path / "agent_output.md").exists()


# ------------------------------------------------------------------ gap 2
def test_build_self_test_catches_buggy_exit0_solution(tmp_path, monkeypatch):
    """solution.py that exits 0 but fails seeded tests must NOT ship."""
    from nine.chains import flagship

    def fake_build(inputs, job_dir):
        (Path(job_dir) / "solution.py").write_text(
            "def add(a, b):\n    return a * b\n\n"
            "if __name__ == '__main__':\n    print('ok')\n",
            encoding="utf-8")
        return {"output": "wrote buggy solution.py (exits 0)"}

    monkeypatch.setattr(flagship, "_build_adk_node",
                        lambda: Node(id="build", kind="tool", run=fake_build))
    hop = flagship.build_hop()
    seed = {"test_solution.py":
            "from solution import add\n"
            "def test_add():\n    assert add(2, 3) == 5\n"}
    res, job, jd = _execute(hop, tmp_path, inputs="make add work", seed=seed)
    # gate keeps FIX; job blocked after max_fix_loops (2) + initial
    assert res["verdict"]["verdict"] == "FIX"
    assert job.status == "blocked"
    ev = json.loads((jd / "EVAL.json").read_text())
    assert ev["checks"][0]["passed"] is False


def test_build_self_test_ships_correct_solution(tmp_path, monkeypatch):
    from nine.chains import flagship

    def fake_build(inputs, job_dir):
        (Path(job_dir) / "solution.py").write_text(
            "def add(a, b):\n    return a + b\n\n"
            "if __name__ == '__main__':\n    print(add(2, 3))\n",
            encoding="utf-8")
        return {"output": "wrote correct solution.py"}

    monkeypatch.setattr(flagship, "_build_adk_node",
                        lambda: Node(id="build", kind="tool", run=fake_build))
    hop = flagship.build_hop()
    seed = {"test_solution.py":
            "from solution import add\n"
            "def test_add():\n    assert add(2, 3) == 5\n"}
    res, job, jd = _execute(hop, tmp_path, inputs="make add work", seed=seed)
    assert res["verdict"]["verdict"] == "SHIP", res["verdict"]
    ev = json.loads((jd / "EVAL.json").read_text())
    assert ev["checks"][0]["passed"] is True


# ------------------------------------------------------------------ gap 3
def test_debug_ships_perfect_patch_without_root_cause(tmp_path, monkeypatch):
    from nine.workflows import debug_wf

    def fake_diag(inputs, job_dir):
        # diagnose produces NOTHING (no ROOT_CAUSE.md)
        return {"output": "no diagnosis written"}

    def fake_patch(inputs, job_dir):
        (Path(job_dir) / "patch.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8")
        return {"output": "wrote patch.py"}

    monkeypatch.setattr(debug_wf, "_diagnose_adk_node",
                        lambda: Node(id="diagnose", kind="tool", run=fake_diag))
    monkeypatch.setattr(debug_wf, "_patch_adk_node",
                        lambda: Node(id="patch", kind="tool", run=fake_patch))
    hop = debug_wf.debug_hop()
    seed = {"test_solution.py":
            "from patch import add\n"
            "def test_add():\n    assert add(2, 3) == 5\n"}
    res, job, jd = _execute(hop, tmp_path, inputs="fix add", seed=seed)
    assert res["verdict"]["verdict"] == "SHIP", res["verdict"]
    assert not (jd / "ROOT_CAUSE.md").exists()
    assert (jd / "patch.py").exists()


# ------------------------------------------------------------------ gap 7
def test_collection_error_reports_clear_message(tmp_path, monkeypatch):
    """Broken test file (collection error) must not report 0/0 counts."""
    from nine.workflows import debug_wf

    def fake_diag(inputs, job_dir):
        return {"output": "no diagnosis written"}

    def fake_patch(inputs, job_dir):
        (Path(job_dir) / "patch.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8")
        return {"output": "wrote patch.py"}

    monkeypatch.setattr(debug_wf, "_diagnose_adk_node",
                        lambda: Node(id="diagnose", kind="tool", run=fake_diag))
    monkeypatch.setattr(debug_wf, "_patch_adk_node",
                        lambda: Node(id="patch", kind="tool", run=fake_patch))
    hop = debug_wf.debug_hop()
    seed = {"test_solution.py": "def broken(:"}  # SyntaxError -> collection error
    res, job, jd = _execute(hop, tmp_path, inputs="fix add", seed=seed)
    ev = json.loads((jd / "EVAL.json").read_text())
    msg = ev["checks"][0]["message"]
    assert "collection error" in msg, msg
    assert "0 test(s) failed" not in msg


# ------------------------------------------------------------------ gap 4
def test_cli_router_keyword_only_without_key(tmp_path, monkeypatch):
    """Without GEMINI_API_KEY the CLI router stays on the keyword substrate."""
    from nine.cli import build_default_router

    monkeypatch.setenv("GEMINI_API_KEY", "")
    r = build_default_router()
    assert r.model_router is None
    d = r.classify("please fix the bug in add")
    assert d.workflow_id == "debug"


def test_cli_router_uses_model_when_available(tmp_path, monkeypatch):
    """With a model wired, classify delegates to the model first."""
    from nine import cli

    class FakeModel:
        def generate_content(self, prompt):
            from types import SimpleNamespace
            return SimpleNamespace(
                text='{"workflow_id": "build", "confidence": 0.95, '
                     '"reason": "implement means build"}')

    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    def _routing_model():
        return FakeModel()

    monkeypatch.setattr(cli, "_routing_model", _routing_model)
    r = cli.build_default_router()
    assert r.model_router is not None
    d = r.classify("implementation of the sort feature")
    assert d.workflow_id == "build"
    assert d.model != "deterministic-keyword"
