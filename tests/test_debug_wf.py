"""Debug workflow tests - hermetic, model-or-fail.

Tests inject fake ADK nodes via monkeypatch; without GEMINI_API_KEY the
real nodes fail loud (WorkflowError).
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
from nine.workflows.debug_wf import (
    debug_hop as make_debug_hop,
)


def _install_fake_debug_nodes(monkeypatch):
    """Replace ADK diagnose+patch nodes with hermetic ones that write real files."""
    from nine.workflows import debug_wf

    def fake_diagnose_run(inputs, job_dir):
        job_dir = Path(job_dir)
        (job_dir / "ROOT_CAUSE.md").write_text(
            "# Root Cause Analysis\n\n"
            "## Symptom\nThe add function has a bug.\n\n"
            "## Root Cause\nThe function returns a - b instead of a + b.\n\n"
            "## Fix Plan\nChange the operator from - to +.\n\n"
            "## Risk\nNone.\n",
            encoding="utf-8",
        )
        return {"output": "wrote ROOT_CAUSE.md"}

    def fake_patch_run(inputs, job_dir):
        job_dir = Path(job_dir)
        # Write a fixed version (add instead of subtract)
        (job_dir / "patch.py").write_text(
            "def add(a, b):\n    return a + b\n",
            encoding="utf-8",
        )
        return {"output": "wrote patch.py"}

    monkeypatch.setattr(
        debug_wf, "_diagnose_adk_node",
        lambda: Node(id="diagnose", kind="tool", run=fake_diagnose_run,
                     description="fake diagnose (hermetic)"),
    )
    monkeypatch.setattr(
        debug_wf, "_patch_adk_node",
        lambda: Node(id="patch", kind="tool", run=fake_patch_run,
                     description="fake patch (hermetic)"),
    )


def test_debug_hop_ships_with_passing_patch(tmp_path, monkeypatch):
    """debug_hop: diagnose writes ROOT_CAUSE.md, patch writes correct fix -> SHIP."""
    _install_fake_debug_nodes(monkeypatch)
    hop = make_debug_hop()
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("debug", {"task": "fix the add function"})

    # Pre-seed a broken solution + test so verify can run pytest
    job_dir_pre = tmp_path / "work" / job.job_id
    job_dir_pre.mkdir(parents=True)
    (job_dir_pre / "solution.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8")
    (job_dir_pre / "test_solution.py").write_text(
        "from solution import add\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n",
        encoding="utf-8")

    res = ex.execute(hop.workflow, job, {"task": "fix the add function"})
    assert res["verdict"]["verdict"] == "SHIP"
    job_dir = tmp_path / "work" / job.job_id
    assert (job_dir / "ROOT_CAUSE.md").exists()
    assert (job_dir / "patch.py").exists()
    assert (job_dir / "EVAL.json").exists()
    ev = json.loads((job_dir / "EVAL.json").read_text())
    assert ev["checks"][0]["passed"] is True


def test_debug_hop_fix_loop_when_patch_fails(tmp_path, monkeypatch):
    """debug_hop: first attempt writes a wrong patch, retry writes correct -> SHIP."""
    _install_fake_debug_nodes(monkeypatch)
    from nine.workflows import debug_wf
    calls = {"n": 0}

    def flaky_patch_run(inputs, job_dir):
        calls["n"] += 1
        job_dir = Path(job_dir)
        if calls["n"] == 1 and not inputs.get("fix_directive"):
            # Write a still-broken patch on first attempt
            (job_dir / "patch.py").write_text(
                "def add(a, b):\n    return a * b\n", encoding="utf-8")
        else:
            # Write correct patch on retry
            (job_dir / "patch.py").write_text(
                "def add(a, b):\n    return a + b\n", encoding="utf-8")
        return {"output": "wrote patch.py"}

    monkeypatch.setattr(
        debug_wf, "_patch_adk_node",
        lambda: Node(id="patch", kind="tool", run=flaky_patch_run, max_retries=0,
                     description="fake patch (flaky)"),
    )

    hop = make_debug_hop()
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("debug", {"task": "fix the add function"})

    job_dir_pre = tmp_path / "work" / job.job_id
    job_dir_pre.mkdir(parents=True)
    (job_dir_pre / "solution.py").write_text(
        "def add(a, b):\n    return a - b\n", encoding="utf-8")
    (job_dir_pre / "test_solution.py").write_text(
        "from solution import add\n"
        "def test_add():\n"
        "    assert add(2, 3) == 5\n",
        encoding="utf-8")

    res = ex.execute(hop.workflow, job, {"task": "fix the add function"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert calls["n"] >= 2


def test_debug_hop_fails_loud_without_api_key(tmp_path):
    """Model-or-fail: without GEMINI_API_KEY, the diagnose node raises WorkflowError."""
    assert not os.environ.get("GEMINI_API_KEY")
    hop = make_debug_hop()
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("debug", {"task": "fix the crash"})
    with pytest.raises(WorkflowError) as exc_info:
        ex.execute(hop.workflow, job, {"task": "fix the crash"})
    assert "GEMINI_API_KEY" in str(exc_info.value)
    assert job.status == "failed"


def test_debug_hop_in_chain(tmp_path, monkeypatch):
    """debug_hop works in a chain: build -> debug (build writes broken code,
    debug diagnoses + patches it, verify runs patch.py directly)."""
    from nine.chains import flagship
    from nine.chains.flagship import build_hop

    # Fake build: writes a BROKEN solution (subtracts instead of adds)
    def fake_build_run(inputs, job_dir):
        (Path(job_dir) / "solution.py").write_text(
            "def add(a, b):\n    return a - b\n", encoding="utf-8")
        (Path(job_dir) / "test_solution.py").write_text(
            "from solution import add\ndef test_add():\n    assert isinstance(add(2, 3), int)\n", encoding="utf-8")
        return {"output": "wrote solution.py + test_solution.py"}

    monkeypatch.setattr(
        flagship, "_build_adk_node",
        lambda: Node(id="build", kind="tool", run=fake_build_run,
                     description="fake ADK (hermetic, writes broken code)"),
    )

    # Fake debug nodes: diagnose writes ROOT_CAUSE.md, patch writes correct code
    _install_fake_debug_nodes(monkeypatch)

    chain = Chain(
        id="build-debug",
        hops=[build_hop(), make_debug_hop()],
        description="Build -> debug",
    )

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = ChainExecutor(ledger, workdir=tmp_path / "work")
    job = ledger.submit("build-debug",
                        {"task": "fix the add function (should add, not subtract)"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "task.txt").write_text(
        "fix the add function (should add, not subtract)\n")

    res = ex.execute(chain, job,
                     {"task": "fix the add function (should add, not subtract)"})
    assert res["final"] == "SHIPPED"
    assert (job_dir / "solution.py").exists()
    assert (job_dir / "ROOT_CAUSE.md").exists()
    assert (job_dir / "patch.py").exists()
    assert (job_dir / "EVAL.json").exists()
