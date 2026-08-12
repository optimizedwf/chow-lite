"""build-multi workflow tests - hermetic, model-or-fail.

Tests inject fake ADK node via monkeypatch; without GEMINI_API_KEY the
real node fails loud (WorkflowError).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
os.environ["GEMINI_API_KEY"] = ""

from nine.chains.chain import Chain, ChainExecutor
from nine.gates.evidence import (
    EvidenceGate,
    eval_json_check,
    exit_codes_check,
    required_artifact_check,
)
from nine.ledger.ledger import JSONLLedger
from nine.runtime.workflows import Node, WorkflowError, WorkflowExecutor
from nine.workflows.build_multi_wf import (
    _build_multi_adk_node,
    build_multi_hop,
)


def _fake_project_run(correct: bool = True):
    """Factory for a hermetic ADK node that scaffolds solution/."""
    def fake_run(inputs, job_dir):
        job_dir = Path(job_dir)
        sol = job_dir / "solution"
        sol.mkdir(exist_ok=True)
        if correct:
            add_body = "return a + b"
        else:
            add_body = "return a * b"
        (sol / "main.py").write_text(
            "from core import add\n\n"
            "def main():\n"
            "    print(add(2, 3))\n\n"
            "if __name__ == '__main__':\n"
            "    main()\n",
            encoding="utf-8")
        (sol / "core.py").write_text(
            f"def add(a, b):\n    {add_body}\n",
            encoding="utf-8")
        (sol / "__init__.py").write_text(
            "from .core import add\n",
            encoding="utf-8")
        (sol / "test_main.py").write_text(
            "from main import add\n"
            "def test_add_positive():\n"
            "    assert add(2, 3) == 5\n"
            "def test_add_zero():\n"
            "    assert add(0, 0) == 0\n"
            "def test_add_negative():\n"
            "    assert add(-1, 1) == 0\n",
            encoding="utf-8")
        return {"output": "scaffolded solution/"}
    return fake_run


def _install_fake_build_multi(monkeypatch, correct=True, flaky=False):
    """Replace the ADK build-multi node with a hermetic one."""
    from nine.workflows import build_multi_wf

    state = {"calls": 0}

    def fake_run(inputs, job_dir):
        state["calls"] += 1
        fix_dir = inputs.get("fix_directive", "")
        # flaky: first call (no directive) writes broken, retries write good
        if flaky and state["calls"] == 1 and not fix_dir:
            return _fake_project_run(correct=False)(inputs, job_dir)
        return _fake_project_run(correct=correct)(inputs, job_dir)

    monkeypatch.setattr(
        build_multi_wf, "_build_multi_adk_node",
        lambda: Node(id="build-multi", kind="tool", run=fake_run,
                     description="fake ADK (hermetic)"),
    )
    return state


def _make_gate(hop):
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)
    return gate


def test_build_multi_ships_with_correct_project(tmp_path, monkeypatch):
    """A correct multi-file project verifies and SHIPs."""
    _install_fake_build_multi(monkeypatch, correct=True)
    hop = build_multi_hop()
    gate = _make_gate(hop)

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("build-multi", {"task": "build a calculator package"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "task.txt").write_text("build a calculator package\n")

    res = ex.execute(hop.workflow, job, {"task": "build a calculator package"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert (job_dir / "solution" / "main.py").exists()
    assert (job_dir / "solution" / "core.py").exists()
    assert (job_dir / "solution" / "__init__.py").exists()
    assert (job_dir / "solution" / "test_main.py").exists()
    assert (job_dir / "EVAL.json").exists()
    # verify ran the in-package tests, not the fallback path
    assert (job_dir / "test_output.log").exists()


def test_build_multi_fix_loop_when_project_broken(tmp_path, monkeypatch):
    """A broken project FIXes; retry writes a correct one and SHIPs."""
    state = _install_fake_build_multi(monkeypatch, correct=True, flaky=True)
    hop = build_multi_hop()
    gate = _make_gate(hop)

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("build-multi", {"task": "build a calculator package"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "task.txt").write_text("build a calculator package\n")

    res = ex.execute(hop.workflow, job, {"task": "build a calculator package"})
    assert state["calls"] >= 2, "fix loop should have retried the build"
    assert res["verdict"]["verdict"] == "SHIP"
    assert res["attempts"] == state["calls"]


def test_build_multi_fails_loud_without_api_key(tmp_path):
    """Without GEMINI_API_KEY the real ADK node raises WorkflowError."""
    hop = build_multi_hop()
    gate = _make_gate(hop)
    real_node = _build_multi_adk_node()

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("build-multi", {"task": "build a calculator package"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)

    with pytest.raises(WorkflowError):
        ex.execute(hop.workflow, job, {"task": "build a calculator package"})


def test_build_multi_in_chain(tmp_path, monkeypatch):
    """build-multi chains with test: scaffold then write+run root tests."""
    from nine.workflows import build_multi_wf, test_wf

    # build-multi: correct project (flaky=False)
    def fake_bm_run(inputs, job_dir):
        return _fake_project_run(correct=True)(inputs, job_dir)
    monkeypatch.setattr(
        build_multi_wf, "_build_multi_adk_node",
        lambda: Node(id="build-multi", kind="tool", run=fake_bm_run,
                     description="fake ADK (hermetic)"),
    )

    # test hop: writes test_solution.py importing the solution package
    def fake_test_run(inputs, job_dir):
        job_dir = Path(job_dir)
        (job_dir / "test_solution.py").write_text(
            "from solution import add\n"
            "def test_add_positive():\n"
            "    assert add(2, 3) == 5\n",
            encoding="utf-8")
        return {"output": "wrote test_solution.py"}
    monkeypatch.setattr(
        test_wf, "_test_adk_node",
        lambda: Node(id="test-writer", kind="tool", run=fake_test_run,
                     description="fake test-writer (hermetic)"),
    )

    from nine.workflows.test_wf import test_hop as make_test_hop
    chain = Chain(
        id="build-multi-test",
        hops=[build_multi_hop(), make_test_hop()],
        description="Build multi-file project -> write + run tests",
    )
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = ChainExecutor(ledger, workdir=tmp_path / "work")
    job = ledger.submit("build-multi-test", {"task": "build a calculator package"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "task.txt").write_text("build a calculator package\n")

    res = ex.execute(chain, job, {"task": "build a calculator package"})
    assert res["final"] == "SHIPPED"
    assert (job_dir / "solution" / "core.py").exists()
    assert (job_dir / "test_solution.py").exists()
    assert (job_dir / "EVAL.json").exists()
