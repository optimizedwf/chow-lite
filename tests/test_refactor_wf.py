"""refactor workflow tests - hermetic, model-or-fail.

Tests inject fake planner/apply nodes and a fake diff-gate via monkeypatch;
without GEMINI_API_KEY the real model nodes fail loud (WorkflowError).
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
)
from nine.ledger.ledger import JSONLLedger
from nine.runtime.workflows import Node, WorkflowError, WorkflowExecutor
from nine.workflows.refactor_wf import refactor_hop

GOOD = "def add(a, b):\n    return a + b\n\ndef main():\n    print(add(2, 3))\n\nif __name__ == '__main__':\n    main()\n"
BROKEN = "def add(a, b):\n    return a - b\n\ndef main():\n    print(add(2, 3))\n\nif __name__ == '__main__':\n    main()\n"


def _install_fakes(monkeypatch, correct=True, flaky=False):
    """Replace planner + apply ADK nodes and the diff-gate prompt with hermetic ones."""
    from nine.workflows import refactor_wf

    state = {"apply_calls": 0}

    def fake_planner(inputs, job_dir):
        job_dir = Path(job_dir)
        (job_dir / "REFACTOR_PLAN.md").write_text(
            "# REFACTOR PLAN\n"
            "## Goal\nRestructure add into a clean module.\n"
            "## Behavior Contract\n`add(a, b)` returns a + b; main() prints.\n"
            "## Edit Spec\n- extract helper, keep public API.\n"
            "## Risks\nNone if tests pass.\n",
            encoding="utf-8")
        return {"output": "wrote REFACTOR_PLAN.md"}

    monkeypatch.setattr(
        refactor_wf, "_planner_adk_node",
        lambda: Node(id="planner", kind="tool", run=fake_planner,
                     description="fake planner (hermetic)"))

    monkeypatch.setattr(
        refactor_wf, "_gemini_generate",
        lambda prompt, api_key=None, **kw: "# DIFF\n## Before\n"
                       "`def add(a, b)`\n## After\n"
                       "`def add(a, b)` with extracted helper\n")

    def fake_apply(inputs, job_dir):
        job_dir = Path(job_dir)
        state["apply_calls"] += 1
        if flaky and state["apply_calls"] == 1:
            (job_dir / "refactored.py").write_text(BROKEN, encoding="utf-8")
        elif correct:
            (job_dir / "refactored.py").write_text(GOOD, encoding="utf-8")
        else:
            (job_dir / "refactored.py").write_text(BROKEN, encoding="utf-8")
        return {"output": "wrote refactored.py"}

    monkeypatch.setattr(
        refactor_wf, "_apply_adk_node",
        lambda: Node(id="apply", kind="tool", run=fake_apply,
                     description="fake apply (hermetic)"))
    return state


def _make_gate(hop):
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)
    return gate


def _submit_with_solution(tmp_path, solution=GOOD):
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    gate = _make_gate(refactor_hop())
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("refactor", {"task": "refactor the calculator"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "solution.py").write_text(solution, encoding="utf-8")
    (job_dir / "test_solution.py").write_text(
        "from solution import add\n"
        "def test_add():\n    assert add(2, 3) == 5\n",
        encoding="utf-8")
    return ex, job, job_dir


def test_refactor_ships_with_receipt(tmp_path, monkeypatch):
    """Context -> plan -> diff -> apply -> verify all pass -> SHIP with receipt."""
    _install_fakes(monkeypatch)
    ex, job, job_dir = _submit_with_solution(tmp_path)

    res = ex.execute(refactor_hop().workflow, job,
                     {"task": "refactor the calculator"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert (job_dir / "CONTEXT.md").exists()
    assert (job_dir / "refactor_before.py").exists()
    assert (job_dir / "REFACTOR_PLAN.md").exists()
    assert (job_dir / "DIFF.md").exists()
    assert (job_dir / "refactored.py").exists()
    receipt = (job_dir / "REFACTOR_RECEIPT.json").read_text()
    assert "tests_passed" in receipt
    assert "true" in receipt


def test_refactor_fix_loop_when_refactored_broken(tmp_path, monkeypatch):
    """First apply writes broken refactored.py -> FIX; retry fixes it -> SHIP."""
    state = _install_fakes(monkeypatch, correct=True, flaky=True)
    ex, job, job_dir = _submit_with_solution(tmp_path)

    res = ex.execute(refactor_hop().workflow, job,
                     {"task": "refactor the calculator"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert state["apply_calls"] == 2
    assert (job_dir / "EVAL.json").exists()
    assert "passed" in (job_dir / "EVAL.json").read_text()


def test_refactor_fails_loud_without_api_key(tmp_path):
    """Without GEMINI_API_KEY the real planner node raises WorkflowError."""
    hop = refactor_hop()
    gate = _make_gate(hop)

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("refactor", {"task": "refactor the calculator"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "solution.py").write_text(GOOD, encoding="utf-8")

    with pytest.raises(WorkflowError):
        ex.execute(hop.workflow, job, {"task": "refactor the calculator"})


def test_refactor_in_chain(tmp_path, monkeypatch):
    """build-multi -> refactor: scaffold then restructure + verify behavior."""
    from nine.workflows import build_multi_wf
    from nine.workflows.build_multi_wf import build_multi_hop as make_build_multi_hop

    _install_fakes(monkeypatch)

    def fake_bm_run(inputs, job_dir):
        job_dir = Path(job_dir)
        sol = job_dir / "solution"
        sol.mkdir(exist_ok=True)
        (sol / "main.py").write_text(
            "from core import add\n\n"
            "def main():\n"
            "    print(add(2, 3))\n\n"
            "if __name__ == '__main__':\n"
            "    main()\n", encoding="utf-8")
        (sol / "core.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8")
        (sol / "__init__.py").write_text(
            "from .core import add\n", encoding="utf-8")
        (sol / "test_main.py").write_text(
            "from main import add\n"
            "def test_add_positive():\n"
            "    assert add(2, 3) == 5\n",
            encoding="utf-8")
        return {"output": "scaffolded solution/"}

    monkeypatch.setattr(
        build_multi_wf, "_build_multi_adk_node",
        lambda: Node(id="build-multi", kind="tool", run=fake_bm_run,
                     description="fake ADK (hermetic)"))

    chain = Chain(
        id="build-refactor",
        hops=[make_build_multi_hop(), refactor_hop()],
        description="Build a project -> refactor it",
    )
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = ChainExecutor(ledger, workdir=tmp_path / "work")
    job = ledger.submit("build-refactor", {"task": "build then refactor"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "task.txt").write_text("build then refactor\n")

    res = ex.execute(chain, job, {"task": "build then refactor"})
    assert res["final"] == "SHIPPED"
    assert (job_dir / "refactored.py").exists()
    assert (job_dir / "REFACTOR_RECEIPT.json").exists()
