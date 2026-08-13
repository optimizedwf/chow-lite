"""Test workflow tests — hermetic, model-or-fail.

Tests inject fake ADK node via monkeypatch; without GEMINI_API_KEY the
real node fails loud (WorkflowError).
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

os.environ["GEMINI_API_KEY"] = ""

from nine.chains.chain import Chain, ChainExecutor
from nine.gates.evidence import (
    EvidenceGate,
)
from nine.ledger.ledger import JSONLLedger
from nine.runtime.workflows import Node, WorkflowError, WorkflowExecutor
from nine.workflows.test_wf import test_hop as make_test_hop


def _install_fake_test_writer(monkeypatch):
    """Replace the ADK test-writer node with a hermetic one that writes
    real test_solution.py + a solution.py (so pytest can actually pass)."""
    from nine.workflows import test_wf

    def fake_run(inputs, job_dir):
        job_dir = Path(job_dir)
        # Write a simple solution for black-box tests
        if not (job_dir / "solution.py").exists():
            (job_dir / "solution.py").write_text(
                "def add(a, b):\n    return a + b\n", encoding="utf-8")
        # Write real pytest tests
        (job_dir / "test_solution.py").write_text(
            "from solution import add\n"
            "def test_add_positive():\n"
            "    assert add(2, 3) == 5\n"
            "def test_add_zero():\n"
            "    assert add(0, 0) == 0\n"
            "def test_add_negative():\n"
            "    assert add(-1, 1) == 0\n",
            encoding="utf-8",
        )
        return {"output": "wrote test_solution.py"}

    monkeypatch.setattr(
        test_wf, "_test_adk_node",
        lambda: Node(id="test-writer", kind="tool", run=fake_run,
                     description="fake test-writer (hermetic)"),
    )


def test_test_hop_ships_when_tests_pass(tmp_path, monkeypatch):
    """test_hop: fake writer writes passing tests -> pytest exits 0 -> SHIP."""
    _install_fake_test_writer(monkeypatch)
    hop = make_test_hop()
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")

    job = ledger.submit("test", {"task": "write tests"})
    res = ex.execute(hop.workflow, job, {"task": "write tests"})
    assert res["verdict"]["verdict"] == "SHIP"
    job_dir = tmp_path / "work" / job.job_id
    assert (job_dir / "test_solution.py").exists()
    assert (job_dir / "EVAL.json").exists()
    ev = json.loads((job_dir / "EVAL.json").read_text())
    assert ev["checks"][0]["passed"] is True


def test_test_hop_fixes_when_tests_fail(tmp_path, monkeypatch):
    """test_hop: fake writer writes failing tests first, passing on retry."""
    _install_fake_test_writer(monkeypatch)
    # Override the fake to write a failing test on first attempt
    from nine.workflows import test_wf
    calls = {"n": 0}

    def flaky_test_writer(inputs, job_dir):
        calls["n"] += 1
        job_dir = Path(job_dir)
        if not (job_dir / "solution.py").exists():
            (job_dir / "solution.py").write_text(
                "def add(a, b):\n    return a + b\n", encoding="utf-8")
        if calls["n"] == 1 and not inputs.get("fix_directive"):
            # Write a failing test on first run
            (job_dir / "test_solution.py").write_text(
                "from solution import add\n"
                "def test_wrong():\n"
                "    assert add(2, 3) == 999\n",
                encoding="utf-8",
            )
        else:
            # Write passing tests on retry
            (job_dir / "test_solution.py").write_text(
                "from solution import add\n"
                "def test_add():\n"
                "    assert add(2, 3) == 5\n",
                encoding="utf-8",
            )
        return {"output": "wrote test_solution.py"}

    monkeypatch.setattr(
        test_wf, "_test_adk_node",
        lambda: Node(id="test-writer", kind="tool", run=flaky_test_writer,
                     max_retries=0, description="fake test-writer (hermetic)"),
    )

    hop = make_test_hop()
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    job_dir = tmp_path / "work" / "test-job"
    job_dir.mkdir(parents=True)
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("test", {"task": "write tests"})
    res = ex.execute(hop.workflow, job, {"task": "write tests"})
    # First attempt: tests fail -> FIX. Second attempt: tests pass -> SHIP.
    assert res["verdict"]["verdict"] == "SHIP"
    assert calls["n"] >= 2


def test_test_hop_fails_loud_without_api_key(tmp_path):
    """Model-or-fail: without GEMINI_API_KEY, the test-writer raises WorkflowError."""
    # Ensure no key
    assert not os.environ.get("GEMINI_API_KEY")
    hop = make_test_hop()
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("test", {"task": "write tests"})
    with pytest.raises(WorkflowError) as exc_info:
        ex.execute(hop.workflow, job, {"task": "write tests"})
    assert "GEMINI_API_KEY" in str(exc_info.value)
    assert job.status == "failed"


def test_test_hop_in_chain(tmp_path, monkeypatch):
    """test_hop works as a chain hop: build -> test."""
    _install_fake_test_writer(monkeypatch)
    # Fake build hop (already pattern in test_chains.py)
    from nine.chains import flagship
    from nine.chains.flagship import build_hop

    def fake_build_run(inputs, job_dir):
        (Path(job_dir) / "solution.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8")
        (Path(job_dir) / "test_solution.py").write_text(
            "from solution import add\ndef test_add():\n    assert add(2, 3) == 5\n", encoding="utf-8")
        return {"output": "wrote solution.py + test_solution.py"}

    monkeypatch.setattr(
        flagship, "_build_adk_node",
        lambda: Node(id="build", kind="tool", run=fake_build_run,
                     description="fake ADK (hermetic)"),
    )

    chain = Chain(
        id="build-test",
        hops=[build_hop(), make_test_hop()],
        description="Build then test",
    )
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = ChainExecutor(ledger, workdir=tmp_path / "work")
    job = ledger.submit("build-test", {"task": "build and test a calculator"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "task.txt").write_text("build and test a calculator\n")
    res = ex.execute(chain, job, {"task": "build and test a calculator"})
    assert res["final"] == "SHIPPED"
    assert (job_dir / "solution.py").exists()
    assert (job_dir / "test_solution.py").exists()
    assert (job_dir / "EVAL.json").exists()
