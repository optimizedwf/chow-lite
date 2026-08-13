"""Regression tests for torture harvest round 4 (2026-08-13, slice 25).

Workers: torture-5 (workflows + router + CLI + docs) + torture-6 (robustness
+ fixtures) on DS4 Flash. Covers the implemented findings:

T5-F2  demo chain keywords removed from production routing
T5-F3  blocked chain marks its container job 'blocked' (recoverable)
T5-F4  recover refuses when task.txt is missing (redacted-task re-execution)
T5-F5  standalone plan gates on PLAN.md only (HANDOFF.md stays chain-only)
T5-F6  every GEMINI_API_KEY guard strips whitespace (shared fsafety too)
T5-F7  NaN/Infinity model confidence falls back to keyword routing
T5-F8  README no longer lies about test counts / research artifacts
T6-F1  symlinked artifacts are never evidence (read side)
T6-F2  a non-UTF8 ledger byte no longer bricks every command
T6-F3  valid-JSON garbage ledger lines are skipped, not tracebacked
T6-F4  redact() covers JSON-quoted, AWS, Slack, and == tail shapes
T6-F6  wrong-shape catalog overrides degrade instead of bricking
T6-F7  --workdir works BEFORE the subcommand (parent parser)
T6-F8  exit-code docstring matches code; memory list skips corrupt lines
"""
import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

import os

os.environ["GEMINI_API_KEY"] = ""

from nine.gates.evidence import (
    EvidenceGate,
    eval_json_check,
    load_eval_json,
    required_artifact_check,
)
from nine.ledger.ledger import JSONLLedger, LedgerError
from nine.router.classifier import Router, redact
from nine.runtime.workflows import Node, Workflow, WorkflowError

# ============================================================== T5-F2 router


def test_demo_keywords_removed_from_production_routing():
    """Real user tasks must NOT route into the canned demo chain (T5-F2)."""
    from nine.registry import HOP_DESCRIPTIONS, KEYWORDS

    r = Router()
    for wf_id, kws in KEYWORDS.items():
        r.register(wf_id, kws, HOP_DESCRIPTIONS.get(wf_id, ""))
    for task in ("customer wants a refund on order 123",
                 "book a trip for the family",
                 "help me with my inbox"):
        d = r.classify(task)
        assert d.workflow_id != "inbox-triage-task-report", task
    # the chain is still reachable explicitly (registry CHAINS keeps it)
    from nine.registry import CHAINS

    assert "inbox-triage-task-report" in CHAINS


# ============================================================== T5-F3 chain


def test_blocked_chain_marks_container_job_blocked(tmp_path):
    """A chain that BLOCKs must leave its container job 'blocked' in the
    durable ledger so discover --status blocked finds it and recover works."""
    from nine.chains.chain import Chain, ChainExecutor, Hop

    bad_wf = Workflow(id="bad")
    bad_wf.add_node(Node(id="bad", kind="bash", command="echo 'no artifact'"))
    chain = Chain(
        id="test-block-terminal",
        hops=[Hop(id="bad", workflow=bad_wf, required_artifacts=["NEVER.md"],
                  gate_checks={"need": required_artifact_check(["NEVER.md"])},
                  max_fix_loops=1)],
    )
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = ChainExecutor(ledger, workdir=tmp_path / "work")
    job = ledger.submit("test-block-terminal", {"task": "x"})
    res = ex.execute(chain, job, {"task": "x"})
    assert res["final"] == "BLOCKED"
    # persisted state, not the in-memory object
    fresh = JSONLLedger(tmp_path / "ledger.jsonl")
    assert fresh.get(job.job_id).status == "blocked"
    assert fresh.discover(status="blocked")  # discover finds it
    # and recover accepts it now
    assert fresh.recover(job.job_id).status == "recovered"


# ============================================================== T5-F4 recover


def test_recover_refuses_missing_task_txt(tmp_path, monkeypatch, capsys):
    """recover with no task.txt must refuse loudly, never re-execute the
    REDACTED ledger task (T5-F4)."""
    from nine import cli as nine_cli

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    job = ledger.submit("respond", {"task": "customer password=*** please help"})
    job.status = "failed"
    ledger.update(job)
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)  # no task.txt!

    args = SimpleNamespace(job_id=job.job_id, ledger=str(tmp_path / "ledger.jsonl"),
                           workdir=str(tmp_path / "work"),
                           events=str(tmp_path / "events.jsonl"),
                           memory=str(tmp_path / "memory.jsonl"))
    rc = nine_cli.cmd_recover(args)
    assert rc == 1
    assert "task.txt is missing" in capsys.readouterr().err
    # job still failed; nothing re-executed
    assert JSONLLedger(tmp_path / "ledger.jsonl").get(job.job_id).status == "failed"


class SimpleNamespace:
    def __init__(self, **kw):
        self.__dict__.update(kw)


# ============================================================== T5-F5 plan gate


def test_standalone_plan_gate_does_not_require_handoff():
    """Standalone plan must be able to SHIP on PLAN.md alone (T5-F5)."""
    from nine.registry import workflow_gate

    gate = workflow_gate("plan")
    check_names = {name for name in gate.checks}
    assert "plan-md" in check_names
    assert "handoff-md" not in check_names
    # the chain plan hop keeps the strict handoff requirement
    from nine.chains.flagship import plan_hop

    strict = plan_hop()
    assert "handoff-md" in strict.gate_checks
    assert "HANDOFF.md" in strict.required_artifacts


# ============================================================== T5-F6 key sweep


def test_no_unstripped_gemini_key_guards_in_source():
    """Every GEMINI_API_KEY guard must strip whitespace (T5-F6) - a
    whitespace key must never pass the model-or-fail check."""
    repo = Path(__file__).resolve().parent.parent
    bad = []
    for p in repo.glob("nine/**/*.py"):
        src = p.read_text()
        for m in re.finditer(r'os\.environ\.get\("GEMINI_API_KEY"\)(?![^\n]*\.strip)', src):
            bad.append(f"{p}:{src[:m.start()].count(chr(10)) + 1}")
    assert not bad, f"un-stripped GEMINI_API_KEY guards:\n{chr(10).join(bad)}"


def test_whitespace_key_fails_loud_in_workflow(monkeypatch, tmp_path):
    """A whitespace GEMINI_API_KEY must raise WorkflowError from a workflow
    node guard, not construct a client (T5-F6)."""

    monkeypatch.setenv("GEMINI_API_KEY", "   ")

    def fake_run(inputs, job_dir):
        return {"output": "should never run"}

    # analyze_wf's _explore_adk_node is the real guard; the node itself must
    # fail loud on a whitespace key (the sweeping fix touched all guards)
    from nine.workflows.analyze_wf import _explore_adk_node

    node = _explore_adk_node()
    job_dir = tmp_path / "work" / "some-job"
    job_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(WorkflowError, match="GEMINI_API_KEY"):
        node.run({"task": "x"}, str(job_dir))


# ============================================================== T5-F7 NaN conf


def test_nan_confidence_falls_back_to_keywords(tmp_path, monkeypatch):
    """A model emitting NaN/Infinity confidence must NOT poison the ledger -
    keyword fallback instead (T5-F7)."""

    class NaNModel:
        def classify(self, task_red):
            return "research", float("nan"), "model said nan"

    r = Router()
    r.register("research", ["research", "study"], "Produce a findings document.")
    r.register("respond", ["hello"], "Direct answer.")
    r.model_router = NaNModel()
    d = r.classify("study black holes")
    assert d.workflow_id == "research"  # keyword fallback
    assert d.model == "deterministic-keyword"
    assert d.confidence == d.confidence  # not NaN
    assert 0.0 <= d.confidence <= 1.0


# ============================================================== T5-F8 README


def test_readme_no_longer_lies():
    readme = (Path(__file__).resolve().parent.parent / "README.md").read_text()
    assert "25 tests" not in readme and "22 passing tests" not in readme
    assert "research.md + EVAL.json" not in readme
    assert "research.md + HANDOFF.md" in readme


# ============================================================== T6-F1 symlinks


def test_symlink_artifacts_are_not_evidence(tmp_path):
    """A symlinked EVAL.json (or any symlink) must not certify outside
    content as the job's own evidence (T6-F1, read side)."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "EVAL.json").write_text(json.dumps({
        "checks": [{"name": "c", "passed": True}], "exit_code": 0}), encoding="utf-8")

    # gate checks: symlink is treated as missing
    job_dir = tmp_path / "job"
    job_dir.mkdir()
    (job_dir / "EVAL.json").symlink_to(outside / "EVAL.json")
    assert load_eval_json(job_dir) is None  # not valid evidence
    ok, msg = required_artifact_check(["EVAL.json"])({}, job_dir)
    assert not ok and "missing" in msg

    # artifact registration skips symlinks entirely: a symlinked EVAL.json
    # must NEVER appear in the artifact manifest (read side of T3-F7)
    from nine.ledger.ledger import JSONLLedger
    from nine.runtime.workflows import WorkflowExecutor

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    gate = EvidenceGate()
    gate.register_check("eval-json", eval_json_check())
    gate.register_check("artifacts", required_artifact_check(["EVAL.json"]))
    ex = WorkflowExecutor(ledger, gate, workdir=tmp_path / "work")
    job = ledger.submit("sym", {"task": "x"})
    wf = Workflow(id="sym")
    wf.add_node(Node(id="bash1", kind="bash",
                     command="echo hi > real.txt; ln -sf ../outside/EVAL.json EVAL.json"))
    res = ex.execute(wf, job, {"task": "x"})
    names = {a["name"] for a in res["artifacts"]}
    assert "real.txt" in names
    assert "EVAL.json" not in names  # symlink is not evidence
    assert job.status in ("blocked", "failed")  # gate cannot pass on symlink


# ============================================================== T6-F2 utf8


def test_non_utf8_ledger_byte_does_not_brick(tmp_path):
    p = tmp_path / "ledger.jsonl"
    good = json.dumps({"job_id": "j-ok", "workflow_id": "respond", "status": "submitted"})
    good2 = json.dumps({"job_id": "j-ok2", "workflow_id": "respond", "status": "submitted"})
    p.write_bytes(good.encode() + b"\n" + b"\xff\xfe bad bytes line\n" + good2.encode())
    ledger = JSONLLedger(p)
    assert ledger.get("j-ok").status == "submitted"
    assert ledger.get("j-ok2").status == "submitted"
    assert len(ledger.corrupt_lines) >= 1
    st = ledger.stats()
    assert st["total"] == 2 and st["corrupt_lines"] >= 1
    # commands keep working
    j = ledger.submit("respond", {"task": "x"})
    assert ledger.get(j.job_id).status == "submitted"


# ============================================================== T6-F3 schema


def test_garbage_status_record_skipped_not_tracebacked(tmp_path):
    p = tmp_path / "ledger.jsonl"
    p.write_text(
        json.dumps({"job_id": "j-good", "workflow_id": "respond", "status": "shipped"}) + "\n"
        + json.dumps({"job_id": "j-cancel", "workflow_id": "respond", "status": "submitted"}) + "\n"
        + json.dumps({"job_id": "j-bad", "workflow_id": "respond", "status": "banana"}) + "\n"
        + json.dumps({"job_id": "j-bad2", "workflow_id": "respond", "status": "submitted",
                      "artifacts": "NOTALIST"}) + "\n",
        encoding="utf-8",
    )
    ledger = JSONLLedger(p)
    assert ledger.get("j-good").status == "shipped"
    with pytest.raises(LedgerError):
        ledger.get("j-bad")
    with pytest.raises(LedgerError):
        ledger.get("j-bad2")
    # discover/artifacts/cancel never traceback
    assert len(ledger.discover()) == 2
    ledger.cancel("j-cancel")
    assert ledger.get("j-cancel").status == "cancelled"
    assert len(ledger.corrupt_lines) == 2


# ============================================================== T6-F4 redact


def test_redact_covers_quoted_aws_slack_and_comparison_shapes():
    cases = [
        ('{"api_key":"sk-abc123def456ghi789"}', "sk-abc123def456ghi789"),
        ('token = "xoxb-1234567890-abcdefghij"', "xoxb-1234567890-abcdefghij"),
        ("password == hunter2", "hunter2"),
        ("pwd != letmein123", "letmein123"),
        ("aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
         "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"),
        ("aws_access_key_id = AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
        ("AKIAIOSFODNN7EXAMPLE", "AKIAIOSFODNN7EXAMPLE"),
    ]
    for text, secret in cases:
        out = redact(text)
        assert secret not in out, f"leaked {secret!r} from {text!r} -> {out!r}"


# ============================================================== T6-F6 catalog


def test_wrong_shape_catalog_degrades(tmp_path, monkeypatch, capsys):
    import nine.registry as reg

    bad = tmp_path / "catalog.json"
    bad.write_text(json.dumps({
        "keyword_overrides": {"research": "NOTALIST"},
        "description_overrides": ["also", "wrong"],
    }), encoding="utf-8")
    monkeypatch.setattr(reg, "_CATALOG_PATH", bad)
    kw = reg._merged_keywords()
    assert "research" in kw and isinstance(kw["research"], list)
    desc = reg._merged_descriptions()
    assert isinstance(desc["research"], str)
    assert "warning" in capsys.readouterr().err


# ============================================================== T6-F7 workdir


def test_workdir_before_subcommand_parses(tmp_path, monkeypatch):
    """--workdir BEFORE the subcommand must parse and be honored (T6-F7)."""
    from nine import cli as nine_cli

    captured = {}

    def fake_submit(args):
        captured["workdir"] = getattr(args, "workdir", None)
        return 0

    monkeypatch.setattr(nine_cli, "cmd_submit", fake_submit)
    assert nine_cli.main(["--workdir", str(tmp_path / "w"), "submit", "t"]) == 0
    assert captured["workdir"] == str(tmp_path / "w")
    # recover subparser must not clobber a pre-subcommand value either
    captured2 = {}

    def fake_recover(args):
        captured2["workdir"] = getattr(args, "workdir", None)
        return 0

    monkeypatch.setattr(nine_cli, "cmd_recover", fake_recover)
    assert nine_cli.main(["--workdir", str(tmp_path / "w2"), "recover", "j-1"]) == 0
    assert captured2["workdir"] == str(tmp_path / "w2")


# ============================================================== T6-F8 docs + memory


def test_exit_code_docstring_matches_code():
    cli_src = (Path(__file__).resolve().parent.parent / "nine" / "cli.py").read_text()
    assert "2 non-SHIP verdict" in cli_src


def test_memory_list_skips_corrupt_line(tmp_path, monkeypatch, capsys):
    from nine import cli as nine_cli

    mem = tmp_path / "memory.jsonl"
    mem.write_text(
        json.dumps({"memory_id": "m-1", "chain_id": "c", "hop_id": "h",
                    "artifact_name": "a.md", "verdict": "SHIP"}) + "\n"
        + "this is not json\n"
        + json.dumps({"memory_id": "m-2", "chain_id": "c", "hop_id": "h",
                      "artifact_name": "b.md", "verdict": "SHIP"}) + "\n",
        encoding="utf-8",
    )
    args = SimpleNamespace(action="list", query=None, memory=str(mem),
                           ledger=str(tmp_path / "l.jsonl"),
                           events=str(tmp_path / "e.jsonl"), workdir="work")
    assert nine_cli.cmd_memory(args) == 0
    out = capsys.readouterr().out
    assert "m-1" in out and "m-2" in out and "2 memory entries" in out
