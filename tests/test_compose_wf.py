"""compose workflow tests - hermetic, model-or-fail.

The three ADK tool nodes (spec / structure-design / implement) are faked;
the bash test/register/validate nodes run FOR REAL against a temp plugin
registry (NINE_PLUGIN_REGISTRY) so the whole meta-loop is exercised:
generate plugin -> compile/import -> register -> fresh-registry validate.
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
from nine.workflows import compose_wf

WFID = "hello_echo"
PLUGIN_NAME = f"{WFID}_wf.py"


def _gen_plugin(wfid=WFID, desc="echo hello", broken=False):
    body = (compose_wf._PLUGIN_TEMPLATE
            .replace("__WF_ID__", wfid)
            .replace("__DESCRIPTION__", desc))
    if broken:
        # unterminated def -> SyntaxError on py_compile
        body = body.replace(f"def {wfid}_hop()", f"def {wfid}_hop(")
    return body


def _make_fakes(mode="good"):
    """Return (spec, structure, implement, state). mode: good|flaky|always-bad."""
    state = {"calls": 0, "written": []}

    def spec(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "SPEC.md").write_text(
            "# SPEC\n\nhello_echo: capture + validate\n", encoding="utf-8")
        (jd / "WF_ID.txt").write_text(WFID, encoding="utf-8")
        return {"output": "spec done"}

    def structure(inputs, job_dir):
        jd = Path(job_dir)
        (jd / "HOP_SPEC.json").write_text(
            '{"workflow_id": "' + WFID + '", "description": "echo hello", '
            '"nodes": [{"id": "capture", "kind": "bash", '
            '"description": "Write DATA.md"}, '
            '{"id": "validate", "kind": "bash", '
            '"description": "Write EVAL.json"}], '
            '"required_artifacts": ["DATA.md", "EVAL.json"], '
            '"max_fix_loops": 2}',
            encoding="utf-8")
        return {"output": "structure done"}

    def implement(inputs, job_dir):
        state["calls"] += 1
        jd = Path(job_dir)
        broken = (mode == "always-bad") or (mode == "flaky" and state["calls"] == 1)
        body = _gen_plugin(broken=broken)
        repo_p = compose_wf._PLUGINS_DIR / PLUGIN_NAME
        job_p = jd / PLUGIN_NAME
        repo_p.write_text(body, encoding="utf-8")
        job_p.write_text(body, encoding="utf-8")
        state["written"] += [repo_p, job_p]
        return {"output": f"implement done (call {state['calls']})"}

    def node(pid, fn):
        return Node(id=pid, kind="tool", run=fn)

    return (node("spec", spec), node("structure-design", structure),
            node("implement", implement), state)


@pytest.fixture
def plugins(tmp_path, monkeypatch):
    reg_file = tmp_path / "plugin_registry.py"
    reg_file.write_text(
        'PLUGIN_WORKFLOWS: dict[str, object] = {}\n', encoding="utf-8")
    monkeypatch.setenv("NINE_PLUGIN_REGISTRY", str(reg_file))
    # remove any leftover generated plugin from a previous run
    leftover = compose_wf._PLUGINS_DIR / PLUGIN_NAME
    if leftover.exists():
        leftover.unlink()
    yield reg_file
    leftover = compose_wf._PLUGINS_DIR / PLUGIN_NAME
    if leftover.exists():
        leftover.unlink()


def _make_gate(hop):
    gate = EvidenceGate()
    for name, check in hop.gate_checks.items():
        gate.register_check(name, check)
    return gate


def _submit(tmp_path, plugins, mode="good"):
    spec_fn, structure_fn, implement_fn, state = _make_fakes(mode)
    hop = compose_wf.compose_hop()
    # swap the three ADK tool nodes for fakes
    for n in hop.workflow.nodes.values():
        if n.id == "spec":
            n.run = spec_fn.run
        elif n.id == "structure-design":
            n.run = structure_fn.run
        elif n.id == "implement":
            n.run = implement_fn.run
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    gate = _make_gate(hop)
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("compose", {"task": "build a hello_echo workflow"})
    return ex, job, hop, state


def test_compose_ships_generated_workflow(tmp_path, plugins):
    ex, job, hop, state = _submit(tmp_path, plugins, mode="good")
    res = ex.execute(hop.workflow, job, {"task": "build a hello_echo workflow"})
    assert res["verdict"]["verdict"] == "SHIP", res["verdict"]
    # plugin landed in the repo plugins dir + job dir
    repo_p = compose_wf._PLUGINS_DIR / PLUGIN_NAME
    assert repo_p.exists() and repo_p.stat().st_size >= 100
    # registry: the plugin factory line is in the temp registry
    reg_txt = plugins.read_text(encoding="utf-8")
    assert f"from nine.chains.plugins.{WFID}_wf import {WFID}_hop" in reg_txt
    assert f'PLUGIN_WORKFLOWS["{WFID}"]' in reg_txt
    # validate node proved it in a FRESH registry import
    ev = (tmp_path / "work" / job.job_id / "EVAL.json").read_text(encoding="utf-8")
    assert '"passed": true' in ev


def test_compose_fix_loop_recovers_bad_first_plugin(tmp_path, plugins):
    ex, job, hop, state = _submit(tmp_path, plugins, mode="flaky")
    res = ex.execute(hop.workflow, job, {"task": "build a hello_echo workflow"})
    assert res["verdict"]["verdict"] == "SHIP", res["verdict"]
    assert res["attempts"] == 2
    assert state["calls"] == 2


def test_compose_blocks_on_always_bad_plugin(tmp_path, plugins):
    ex, job, hop, state = _submit(tmp_path, plugins, mode="always-bad")
    res = ex.execute(hop.workflow, job, {"task": "build a hello_echo workflow"})
    # the gate keeps returning FIX; the JOB status is the source of truth
    assert res["verdict"]["verdict"] == "FIX"
    assert job.status == "blocked"
    assert res["attempts"] == 3  # initial + max_fix_loops(2)


def test_compose_fails_loud_without_api_key(tmp_path, plugins):
    hop = compose_wf.compose_hop()
    gate = _make_gate(hop)
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("compose", {"task": "build a hello_echo workflow"})
    with pytest.raises(WorkflowError):
        ex.execute(hop.workflow, job, {"task": "build a hello_echo workflow"})
