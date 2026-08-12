"""document workflow tests - hermetic, model-or-fail.

Tests inject a fake docgen node via monkeypatch; without GEMINI_API_KEY
the real ADK node fails loud (WorkflowError).
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
from nine.workflows.document_wf import document_hop

GOOD = "def add(a, b):\n    return a + b\n\ndef main():\n    print(add(2, 3))\n\nif __name__ == '__main__':\n    main()\n"

README = "# Calc\nA calculator module.\n## Run\n`python3 solution.py`\n"
API = "# API\n- `add(a, b) -> int` adds two numbers.\n"


def _install_fake_docgen(monkeypatch, flaky=False, correct=True):
    """Replace the ADK docgen node with a hermetic one."""
    from nine.workflows import document_wf

    state = {"calls": 0}

    def fake_run(inputs, job_dir):
        job_dir = Path(job_dir)
        state["calls"] += 1
        if flaky and state["calls"] == 1:
            # first attempt: README only (API.md missing -> gate FIX)
            (job_dir / "README.md").write_text(README, encoding="utf-8")
        elif correct:
            (job_dir / "README.md").write_text(README, encoding="utf-8")
            (job_dir / "API.md").write_text(API, encoding="utf-8")
        else:
            (job_dir / "README.md").write_text("", encoding="utf-8")
        return {"output": "wrote docs"}

    monkeypatch.setattr(
        document_wf, "_docgen_adk_node",
        lambda: Node(id="docgen", kind="tool", run=fake_run,
                     description="fake docgen (hermetic)"))
    return state


def _make_gate(hop):
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)
    return gate


def _submit(tmp_path, with_solution=True):
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    gate = _make_gate(document_hop())
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("document", {"task": "document the calculator"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    if with_solution:
        (job_dir / "solution.py").write_text(GOOD, encoding="utf-8")
    return ex, job, job_dir


def test_document_ships_with_readme_and_api(tmp_path, monkeypatch):
    """Inventory -> docgen writes README.md + API.md -> SHIP."""
    _install_fake_docgen(monkeypatch)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(document_hop().workflow, job,
                     {"task": "document the calculator"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert (job_dir / "INVENTORY.md").exists()
    assert "solution.py" in (job_dir / "INVENTORY.md").read_text()
    assert (job_dir / "README.md").read_text() == README
    assert (job_dir / "API.md").read_text() == API


def test_document_fix_loop_when_api_missing(tmp_path, monkeypatch):
    """First docgen writes README only -> FIX; retry adds API.md -> SHIP."""
    state = _install_fake_docgen(monkeypatch, flaky=True)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(document_hop().workflow, job,
                     {"task": "document the calculator"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert state["calls"] == 2
    assert (job_dir / "API.md").exists()


def test_document_fails_loud_without_api_key(tmp_path):
    """Without GEMINI_API_KEY the real docgen node raises WorkflowError."""
    hop = document_hop()
    gate = _make_gate(hop)

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("document", {"task": "document the calculator"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "solution.py").write_text(GOOD, encoding="utf-8")

    with pytest.raises(WorkflowError):
        ex.execute(hop.workflow, job, {"task": "document the calculator"})


def test_document_in_chain_build_multi(tmp_path, monkeypatch):
    """build-multi -> document: scaffold a project then document it."""
    from nine.workflows import build_multi_wf
    from nine.workflows.build_multi_wf import build_multi_hop as make_build_multi_hop

    _install_fake_docgen(monkeypatch)

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
        id="build-document",
        hops=[make_build_multi_hop(), document_hop()],
        description="Build a project -> document it",
    )
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = ChainExecutor(ledger, workdir=tmp_path / "work")
    job = ledger.submit("build-document", {"task": "build then document"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "task.txt").write_text("build then document\n")

    res = ex.execute(chain, job, {"task": "build then document"})
    assert res["final"] == "SHIPPED"
    assert (job_dir / "README.md").exists()
    assert (job_dir / "API.md").exists()
    inventory = (job_dir / "INVENTORY.md").read_text()
    assert "solution" in inventory
