"""analyze workflow tests - hermetic, model-or-fail.

The bash nodes (inspect/visualize) run for real in a tmp job dir; the
model nodes (explore/report) are replaced by fakes.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os

os.environ["GEMINI_API_KEY"] = ""

from nine.gates.evidence import EvidenceGate
from nine.ledger.ledger import JSONLLedger
from nine.runtime.workflows import Node, WorkflowError, WorkflowExecutor
from nine.workflows.analyze_wf import analyze_hop

CSV = "x,y,label\n1,10,a\n2,20,b\n3,30,a\n4,40,b\n5,50,a\n"
EXPLORATION = (
    "# Exploration\n\nPatterns:\n1. y doubles with x.\n"
    "Chart rec: scatter x vs y.\n"
)
INSIGHTS = (
    "# Insights\n\nSummary: linear relationship.\n\n"
    "Key insights:\n1. y = 10*x exactly.\n"
    "Caveats: tiny sample.\nNext: fit a model.\n"
)


def _install_fakes(monkeypatch, chart_ok=True):
    """Replace the two model nodes with hermetic fakes."""
    from nine.workflows import analyze_wf

    def fake_explore(inputs, job_dir):
        (Path(job_dir) / "EXPLORATION.md").write_text(
            EXPLORATION, encoding="utf-8")
        return {"output": "wrote EXPLORATION.md"}

    def fake_report(inputs, job_dir):
        (Path(job_dir) / "INSIGHTS.md").write_text(INSIGHTS, encoding="utf-8")
        return {"output": "wrote INSIGHTS.md"}

    monkeypatch.setattr(analyze_wf, "_explore_adk_node",
                        lambda: Node(id="explore", kind="tool",
                                     run=fake_explore))
    monkeypatch.setattr(analyze_wf, "_report_prompt_node",
                        lambda: Node(id="report", kind="prompt",
                                     run=fake_report))


def _make_gate(hop):
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)
    return gate


def _submit(tmp_path):
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    gate = _make_gate(analyze_hop())
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("analyze", {"task": "analyze the dataset"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "data.csv").write_text(CSV, encoding="utf-8")
    return ex, job, job_dir


def test_analyze_ships_with_chart_and_insights(tmp_path, monkeypatch):
    """inspect -> explore -> visualize -> report -> SHIP."""
    _install_fakes(monkeypatch)
    ex, job, job_dir = _submit(tmp_path)

    res = ex.execute(analyze_hop().workflow, job,
                     {"task": "analyze the dataset"})
    assert res["verdict"]["verdict"] == "SHIP"
    profile = (job_dir / "DATA_PROFILE.md").read_text(encoding="utf-8")
    assert "x" in profile and "y" in profile
    assert (job_dir / "chart.png").stat().st_size >= 1024
    assert "linear" in (job_dir / "INSIGHTS.md").read_text(encoding="utf-8")


def test_analyze_fix_loop_when_chart_stub(tmp_path, monkeypatch):
    """First chart.png is a stub -> FIX; real chart on retry -> SHIP."""
    from nine.workflows import analyze_wf

    state = {"calls": 0}

    def flaky_visualize(inputs, job_dir):
        state["calls"] += 1
        p = Path(job_dir) / "chart.png"
        if state["calls"] == 1:
            p.write_bytes(b"stub")
        else:
            # render a real chart in-process (venv pytest has matplotlib)
            os.environ["MPLBACKEND"] = "Agg"
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import pandas as pd
            df = pd.read_csv(Path(job_dir) / "data.csv")
            fig, ax = plt.subplots()
            ax.scatter(df["x"], df["y"])
            fig.savefig(Path(job_dir) / "chart.png")
            plt.close(fig)
        return {"output": "visualized"}

    def fake_explore(inputs, job_dir):
        (Path(job_dir) / "EXPLORATION.md").write_text(
            EXPLORATION, encoding="utf-8")
        return {"output": "wrote EXPLORATION.md"}

    def fake_report(inputs, job_dir):
        (Path(job_dir) / "INSIGHTS.md").write_text(INSIGHTS, encoding="utf-8")
        return {"output": "wrote INSIGHTS.md"}

    monkeypatch.setattr(analyze_wf, "_explore_adk_node",
                        lambda: Node(id="explore", kind="tool",
                                     run=fake_explore))
    monkeypatch.setattr(analyze_wf, "_report_prompt_node",
                        lambda: Node(id="report", kind="prompt",
                                     run=fake_report))
    monkeypatch.setattr(
        analyze_wf, "_visualize_command",
        lambda: ("true"),  # replaced below at node level; keep real usage
    )
    # Replace the visualize NODE factory instead: monkeypatch workflow Node
    ex, job, job_dir = _submit(tmp_path)
    # re-install: _submit builds a fresh hop; patch the module-level command
    # by directly swapping the node used in the hop
    import nine.workflows.analyze_wf as awf

    # simpler: craft hop with patched visualize via _make_gate on custom hop
    hop = awf.analyze_hop()
    # find visualize node and swap run
    for n in hop.workflow.nodes.values():
        if n.id == "visualize":
            n.kind = "tool"
            n.run = flaky_visualize
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("analyze", {"task": "analyze the dataset"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "data.csv").write_text(CSV, encoding="utf-8")

    res = ex.execute(hop.workflow, job, {"task": "analyze the dataset"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert state["calls"] == 2


def test_analyze_blocks_when_chart_missing(tmp_path, monkeypatch):
    """chart.png absent -> chart check fails -> not SHIP."""
    from nine.workflows import analyze_wf

    def no_chart(inputs, job_dir):
        return {"output": "no chart"}

    def fake_explore(inputs, job_dir):
        (Path(job_dir) / "EXPLORATION.md").write_text(
            EXPLORATION, encoding="utf-8")
        return {"output": "wrote EXPLORATION.md"}

    def fake_report(inputs, job_dir):
        (Path(job_dir) / "INSIGHTS.md").write_text(INSIGHTS, encoding="utf-8")
        return {"output": "wrote INSIGHTS.md"}

    monkeypatch.setattr(analyze_wf, "_explore_adk_node",
                        lambda: Node(id="explore", kind="tool",
                                     run=fake_explore))
    monkeypatch.setattr(analyze_wf, "_report_prompt_node",
                        lambda: Node(id="report", kind="prompt",
                                     run=fake_report))
    hop = analyze_wf.analyze_hop()
    for n in hop.workflow.nodes.values():
        if n.id == "visualize":
            n.kind = "tool"
            n.run = no_chart
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("analyze", {"task": "analyze the dataset"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "data.csv").write_text(CSV, encoding="utf-8")

    res = ex.execute(hop.workflow, job, {"task": "analyze the dataset"})
    assert res["verdict"]["verdict"] != "SHIP"
    assert res["verdict"]["eval_results"]["chart"]["passed"] is False


def test_analyze_fails_loud_without_api_key(tmp_path):
    """Without GEMINI_API_KEY the real explorer raises WorkflowError."""
    hop = analyze_hop()
    gate = _make_gate(hop)

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("analyze", {"task": "analyze the dataset"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "data.csv").write_text(CSV, encoding="utf-8")

    with pytest.raises(WorkflowError):
        ex.execute(hop.workflow, job, {"task": "analyze the dataset"})
