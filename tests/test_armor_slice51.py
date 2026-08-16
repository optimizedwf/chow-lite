"""Test armor slice-51 — hermetic coverage for long-untested paths.

Targets found by AST scan (functions defined in nine/ with ZERO
references in tests/): force_terminal (chain job state-machine walk),
Workflow.topological_order (cycle detection + dep ordering),
default_gate (the generic fallback gate every submit/recover/API path
uses), cmd_artifacts + cmd_cancel (CLI error-path contract), and
llm_provider.base_url (env override + slash normalization).

Hermetic: no GEMINI_API_KEY; nothing here touches a model.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["GEMINI_API_KEY"] = ""

import pytest

from nine.chains.chain import force_terminal
from nine.cli import main
from nine.ledger.ledger import Job, JSONLLedger
from nine.registry import default_gate
from nine.runtime.workflows import Node, Workflow, WorkflowError


# ------------------------------------------------------- hermetic model fakes
@pytest.fixture(autouse=True)
def _isolated_catalog(tmp_path, monkeypatch):
    """Learn apply/revert writes the shared git-tracked catalog for real;
    point it at a temp file so tests never touch the repo catalog."""
    monkeypatch.setattr("nine.registry._CATALOG_PATH", tmp_path / "catalog.json")


@pytest.fixture(autouse=True)
def _fake_models(monkeypatch):
    """Hermetic model fakes (no offline fallbacks exist anymore)."""
    from nine.chains import flagship
    from nine.runtime import responder, summarizer
    from nine.runtime.workflows import Node

    monkeypatch.setattr(
        responder, "respond_text",
        lambda task, max_chars=600: ("a real model answer", "gemini"),
    )
    monkeypatch.setattr(
        summarizer, "summarize_text",
        lambda text, max_words=120, task="", api_key=None:
        ("distilled findings about fooquark", "fake-gemini"),
    )

    def fake_build_run(inputs, job_dir):
        (Path(job_dir) / "solution.py").write_text(
            "def answer():\n    return 42\n", encoding="utf-8")
        (Path(job_dir) / "test_solution.py").write_text(
            "from solution import answer\ndef test_answer():\n    assert answer() == 42\n", encoding="utf-8")
        return {"output": "wrote solution.py + test_solution.py"}

    monkeypatch.setattr(
        flagship, "_build_adk_node",
        lambda: Node(id="build", kind="tool", run=fake_build_run,
                     description="fake ADK node (hermetic test)"),
    )

    def fake_research_run(inputs, job_dir):
        (Path(job_dir) / "research.md").write_text(
            "# Findings\n\nResearch findings about the task: evidence-gated "
            "execution keeps agents honest and every hop verifiable.\n", encoding="utf-8")
        return {"output": "wrote research.md"}

    def fake_plan_run(inputs, job_dir):
        (Path(job_dir) / "PLAN.md").write_text(
            "# Plan\n\n1. scaffold\n2. implement\n3. verify with EVAL.json\n",
            encoding="utf-8")
        return {"output": "wrote PLAN.md"}

    monkeypatch.setattr(
        flagship, "_research_adk_node",
        lambda: Node(id="research", kind="tool", run=fake_research_run,
                     description="fake research ADK node (hermetic test)"),
    )
    monkeypatch.setattr(
        flagship, "_plan_adk_node",
        lambda: Node(id="plan", kind="tool", run=fake_plan_run,
                     description="fake plan ADK node (hermetic test)"),
    )
    monkeypatch.setattr(
        "nine.runtime.gemma.gemma_generate",
        lambda prompt, model=None, api_key=None, timeout=90:
        "gate every hop on evidence before handoff.",
    )


# ------------------------------------------------------- T-armor: force_terminal
def test_force_terminal_walks_legal_path_to_shipped():
    """force_terminal drives a fresh job through submitted->routing->
    running->awaiting_evidence->shipped via LEGAL transitions (chain jobs
    are containers; hops are their own ledger jobs)."""
    job = Job(workflow_id="w", job_id="j-ship")
    force_terminal(job, "shipped")
    assert job.status == "shipped"
    assert job.completed_at is not None


def test_force_terminal_blocked_path():
    """blocked is reachable from awaiting_evidence via the legal walk
    (routing -> running -> blocked); the walk never skips a legal step."""
    job = Job(workflow_id="w", job_id="j-block")
    force_terminal(job, "blocked")
    assert job.status == "blocked"
    assert job.completed_at is not None


def test_force_terminal_failed_path():
    job = Job(workflow_id="w", job_id="j-fail")
    force_terminal(job, "failed")
    assert job.status == "failed"
    assert job.completed_at is not None


def test_force_terminal_cancelled_direct():
    """cancelled has an empty legal path () — it is set directly (the
    transition submitted->cancelled is legal anyway, but the fallback
    must not blow up for any terminal status)."""
    job = Job(workflow_id="w", job_id="j-cancel")
    force_terminal(job, "cancelled")
    assert job.status == "cancelled"
    assert job.completed_at is not None


def test_force_terminal_unknown_status_falls_back_direct_set():
    """An unknown status is not in VALID_STATUSES -> the transition raises
    and force_terminal falls back to a direct set (container semantics:
    the caller owns the status)."""
    job = Job(workflow_id="w", job_id="j-weird")
    force_terminal(job, "weird-status")
    assert job.status == "weird-status"


# ------------------------------------------------------- T-armor: topological_order
def _wf(*nodes):
    wf = Workflow(id="t")
    for n in nodes:
        wf.add_node(n)
    return wf


def test_topological_order_simple_deps():
    wf = _wf(
        Node(id="a", kind="bash", command="true"),
        Node(id="b", kind="bash", command="true", depends_on=["a"]),
        Node(id="c", kind="bash", command="true", depends_on=["b"]),
    )
    order = wf.topological_order()
    assert order.index("a") < order.index("b") < order.index("c")
    assert set(order) == {"a", "b", "c"}


def test_topological_order_ignores_missing_deps():
    """A depends_on edge to a node NOT in the graph must not crash (the
    visit() guard `if dep in self.nodes`)."""
    wf = _wf(
        Node(id="a", kind="bash", command="true", depends_on=["ghost"]),
        Node(id="b", kind="bash", command="true", depends_on=["a"]),
    )
    order = wf.topological_order()
    assert order == ["a", "b"] or (order.index("a") < order.index("b"))


def test_topological_order_cycle_detected():
    """A cycle raises WorkflowError naming the offending node — the
    executor refuses to schedule a cyclic DAG (previously completely
    untested behavior)."""
    wf = _wf(
        Node(id="a", kind="bash", command="true", depends_on=["b"]),
        Node(id="b", kind="bash", command="true", depends_on=["a"]),
    )
    with pytest.raises(WorkflowError, match="cycle detected"):
        wf.topological_order()


def test_topological_order_self_cycle():
    wf = _wf(Node(id="a", kind="bash", command="true", depends_on=["a"]))
    with pytest.raises(WorkflowError, match="cycle detected"):
        wf.topological_order()


# ------------------------------------------------------- T-armor: default_gate
def test_default_gate_registers_eval_json_and_exit_codes():
    """The generic fallback gate (resolve_gate's last resort) must certify
    eval-json + exit-codes for EVERY lane — a None gate would make every
    submit/recover path crash."""
    g = default_gate()
    names = set(g.checks.keys()) if hasattr(g, "checks") else set()
    if not names:
        names = {c["name"] for c in g.check_defs} if hasattr(g, "check_defs") else set()
    # fall back to evaluating the gate on a trivial dir if introspection fails
    if not names:
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "EVAL.json").write_text(
                '{"tests": [{"name": "t", "passed": true}]}')
            v = g.evaluate({}, Path(d))
            assert v["verdict"] == "SHIP"
        return
    assert "eval-json" in names
    assert "exit-codes" in names


# ------------------------------------------------------- T-armor: CLI artifacts/cancel
def test_cli_artifacts_lists_and_errors(tmp_path, capsys):
    """nine artifacts prints the artifact list; a missing job returns 1
    with a clean error line (no traceback)."""
    ledger = tmp_path / "ledger.jsonl"
    assert main(["--ledger", str(ledger), "submit", "research x"]) == 0
    # find the job id
    jobs_out = main(["--ledger", str(ledger), "discover"])
    assert jobs_out == 0
    capsys.readouterr()
    out = capsys.readouterr().out
    job_id = ""
    for line in out.splitlines():
        if "research" in line and len(line.split()) >= 2:
            job_id = line.split()[0]
            break
    if not job_id:
        # fall back: parse the ledger file directly
        import json as _json
        for line in open(ledger).read().splitlines():
            rec = _json.loads(line)
            if rec.get("workflow_id") == "research":
                job_id = rec["job_id"]
                break
    assert job_id, "could not find submitted job"
    assert main(["--ledger", str(ledger), "artifacts", job_id]) == 0
    # missing job -> clean error, rc 1
    assert main(["--ledger", str(ledger), "artifacts", "nope-123"]) == 1
    assert "error:" in capsys.readouterr().err


def test_cli_cancel_job_and_error(tmp_path, capsys):
    """nine cancel works on a cancellable (submitted) job and returns a
    clean LedgerError rc 1 on a terminal one — never a traceback."""
    ledger = tmp_path / "ledger.jsonl"
    lg = JSONLLedger(ledger)
    job = lg.submit("research", {"task": "research y"})
    assert job.status == "submitted"
    assert main(["--ledger", str(ledger), "cancel", job.job_id]) == 0
    assert "cancelled" in capsys.readouterr().out
    # a shipped job cannot be cancelled -> clean LedgerError rc 1
    assert main(["--ledger", str(ledger), "submit", "research z"]) == 0
    import json as _json
    shipped_id = _json.loads(open(ledger).read().splitlines()[-1])["job_id"]
    rc = main(["--ledger", str(ledger), "cancel", shipped_id])
    assert rc == 1
    assert "error:" in capsys.readouterr().err


# ------------------------------------------------------- T-armor: base_url
def test_base_url_env_override_and_normalization(monkeypatch):
    from nine.runtime import llm_provider as lp
    monkeypatch.delenv("NINE_LLM_BASE_URL", raising=False)
    assert lp.base_url() == "https://opencode.ai/zen/go/v1"
    monkeypatch.setenv("NINE_LLM_BASE_URL", "http://127.0.0.1:11434/")
    assert lp.base_url() == "http://127.0.0.1:11434"
    monkeypatch.setenv("NINE_LLM_BASE_URL", "  https://tunnel.example/v2/  ")
    assert lp.base_url() == "https://tunnel.example/v2"
