"""Regression tests for torture harvest round 3 (2026-08-13, slice 24).

Workers: torture-3 (runtime+gates) + torture-4 (robustness+fixtures) on DS4
Flash. Covers the implemented findings:

T4-F1  corrupt/partial ledger line must not brick every `nine` command
T4-F2  `nine recover` on shipped/cancelled must refuse cleanly (no wipe, no
       InvalidTransition traceback) and reset attempts on real recovery
T4-F3  corrupt router catalog must degrade to base keywords, not brick CLI
T4-F4  redaction at the LEDGER boundary (submit + chain + server all covered)
T4-F5  whitespace GEMINI_API_KEY must fail loud (was passing every guard)
T4-F6  global --ledger must survive submit/chain subcommand parsing
T3-F1  debug + build-multi verify must NOT SHIP stubs as 'verified'
T3-F2  standalone review with no EVAL.json must FAIL, never fabricate PASS
T3-F5  node timeout_seconds enforced for callable nodes (was bash-only)
T3-F7  ADK write_file refuses `..` escapes outside the job dir
T3-F8  redact() is case-insensitive (API_KEY=/PASSWORD= redacted)
"""
import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

import os

os.environ["GEMINI_API_KEY"] = ""

from nine.gates.evidence import EvidenceGate
from nine.ledger.ledger import JSONLLedger, LedgerError
from nine.runtime.workflows import Node, WorkflowError, WorkflowExecutor


def _gate(hop):
    g = EvidenceGate()
    for name, check in hop.gate_checks.items():
        g.register_check(name, check)
    return g


def _run_hop(hop, tmp_path, inputs=None, seed=None):
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate=_gate(hop), workdir=tmp_path / "work")
    job = ledger.submit(hop.id, {"task": inputs or "do the thing"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    if seed:
        for name, content in seed.items():
            (job_dir / name).write_text(content, encoding="utf-8")
    res = ex.execute(hop.workflow, job, {"task": inputs or "do the thing"})
    return res, job, job_dir


# ============================================================== T4-F1 ledger


def test_corrupt_ledger_line_does_not_brick(tmp_path):
    """One bad line (truncated write / bad edit) must not kill every command:
    healthy jobs load, the damage is counted, submit still works."""
    p = tmp_path / "ledger.jsonl"
    p.write_text(
        '{"job_id":"j-ok","workflow_id":"respond","status":"submitted"}\n'
        'this is not json\n'
        '["not","a","dict"]\n'
        '{"status":"missing-keys"}\n'
        '{"job_id":"j-ok2","workflow_id":"build","status":"shipped"}\n',
        encoding="utf-8",
    )
    ledger = JSONLLedger(p)
    assert ledger.get("j-ok").status == "submitted"
    assert ledger.get("j-ok2").status == "shipped"
    assert len(ledger.corrupt_lines) == 3
    st = ledger.stats()
    assert st["total"] == 2
    assert st["corrupt_lines"] == 3
    # submit still works after a corrupt line
    j = ledger.submit("respond", {"task": "hello"})
    assert j.status == "submitted"
    assert ledger.get(j.job_id).status == "submitted"


def test_unwritable_ledger_raises_ledger_error(tmp_path):
    """OSError on ledger IO surfaces as LedgerError, not a raw traceback."""
    if os.name == "nt":
        pytest.skip("permission semantics differ on Windows")
    p = tmp_path / "ro.jsonl"
    p.write_text("", encoding="utf-8")
    p.chmod(0o400)
    try:
        ledger = JSONLLedger(p)
        with pytest.raises(LedgerError):
            ledger.submit("respond", {"task": "x"})
    finally:
        p.chmod(0o600)


# ===================================================== T4-F2 / T3-F3 recover


def test_recover_refuses_shipped_job_without_touching_artifacts(tmp_path):
    """recover on a shipped job: clean LedgerError, artifacts untouched."""
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    job = ledger.submit("respond", {"task": "done"})
    job.status = "shipped"
    ledger.update(job)
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "RESPONSE.md").write_text("the verified answer", encoding="utf-8")

    with pytest.raises(LedgerError, match="only blocked/failed"):
        ledger.recover(job.job_id)
    # artifact survived; ledger still says shipped
    assert (job_dir / "RESPONSE.md").exists()
    assert ledger.get(job.job_id).status == "shipped"


def test_recover_resets_attempts_for_full_fix_budget(tmp_path):
    """Recovered jobs get attempts reset so the re-run has a full FIX budget."""
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    job = ledger.submit("respond", {"task": "retry me"})
    job.status = "blocked"
    job.attempts = 3  # already burned the budget
    ledger.update(job)
    recovered = ledger.recover(job.job_id)
    assert recovered.status == "recovered"
    assert recovered.attempts == 0


# ============================================================== T4-F3 catalog


def test_corrupt_catalog_degrades_to_base_keywords(tmp_path, monkeypatch, capsys):
    """A truncated/broken catalog.json must degrade, not brick the CLI."""
    import nine.registry as reg

    bad = tmp_path / "catalog.json"
    bad.write_text('{"keyword_overrides": {', encoding="utf-8")  # truncated
    monkeypatch.setattr(reg, "_CATALOG_PATH", bad)
    data = reg.load_catalog()
    assert data == {"keyword_overrides": {}, "description_overrides": {}}
    assert "warning" in capsys.readouterr().err
    # keyword routing still has the base keyword set
    kw = reg._merged_keywords()
    assert "build" in kw and "test" in kw


def test_non_object_catalog_degrades(tmp_path, monkeypatch, capsys):
    import nine.registry as reg

    bad = tmp_path / "catalog.json"
    bad.write_text("[1,2,3]", encoding="utf-8")
    monkeypatch.setattr(reg, "_CATALOG_PATH", bad)
    assert reg.load_catalog() == {"keyword_overrides": {}, "description_overrides": {}}


# ============================================================== T4-F4 redaction


def test_redaction_at_ledger_boundary_covers_all_submit_paths(tmp_path):
    """The LEDGER redacts, so chain/server submits are covered too."""
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ledger.submit("inbox-triage-task-report",
                  {"task": "the customer PASSWORD=hunter2 and token is sk-ABCDEF1234567890"})
    raw = (tmp_path / "ledger.jsonl").read_text()
    assert "hunter2" not in raw
    assert "ABCDEF1234567890" not in raw
    assert "***" in raw


# ============================================================== T4-F5 whitespace key


def test_whitespace_gemini_key_fails_loud(monkeypatch, tmp_path):
    """GEMINI_API_KEY='   ' must raise the documented WorkflowError."""
    from nine.runtime import responder

    monkeypatch.setenv("GEMINI_API_KEY", "   ")
    calls = []

    class _Client:
        def __init__(self, *a, **k):
            calls.append(k)

    import google.genai as genai_mod  # noqa: E402 - venv dep

    monkeypatch.setattr(genai_mod, "Client", _Client)
    with pytest.raises(WorkflowError, match="GEMINI_API_KEY"):
        responder.respond_text("hi")
    assert calls == []  # never constructed a client with the whitespace key


# ============================================================== T4-F6 argparse


def test_global_ledger_survives_submit_subcommand(tmp_path, monkeypatch):
    """`nine --ledger X submit t` must honor the global flag (T4-F6)."""
    from nine import cli as nine_cli

    captured = {}

    def fake_submit(args):
        captured["ledger"] = args.ledger
        captured["workdir"] = getattr(args, "workdir", "work")
        return 0

    monkeypatch.setattr(nine_cli, "cmd_submit", fake_submit)
    monkeypatch.setattr(nine_cli, "cmd_chain", fake_submit)
    # global flag BEFORE the subcommand
    assert nine_cli.main(["--ledger", "/tmp/X.jsonl", "submit", "t1"]) == 0
    assert captured["ledger"] == "/tmp/X.jsonl"
    # flag AFTER the subcommand still works
    assert nine_cli.main(["submit", "--ledger", "/tmp/Y.jsonl", "t2"]) == 0
    assert captured["ledger"] == "/tmp/Y.jsonl"
    # global flag survives chain subcommand too
    assert nine_cli.main(["--ledger", "/tmp/Z.jsonl", "chain", "demo", "t3"]) == 0
    assert captured["ledger"] == "/tmp/Z.jsonl"


# ============================================================== T3-F1 verify stubs


def test_debug_verify_no_test_evidence_never_ships(tmp_path, monkeypatch):
    """debug verify: a patch.py stub with no tests must not SHIP as verified."""
    from nine.workflows import debug_wf

    def fake_diagnose_run(inputs, job_dir):
        (Path(job_dir) / "ROOT_CAUSE.md").write_text(
            "# Root Cause\n## Symptom\nbroken\n## Root Cause\nx\n## Fix Plan\ny\n## Risk\nz\n",
            encoding="utf-8")
        return {"output": "wrote ROOT_CAUSE.md"}

    def fake_patch_run(inputs, job_dir):
        (Path(job_dir) / "patch.py").write_text(
            "print('TODO: fix the bug')\n", encoding="utf-8")
        return {"output": "wrote patch.py"}

    monkeypatch.setattr(debug_wf, "_diagnose_adk_node",
                        lambda: Node(id="diagnose", kind="tool", run=fake_diagnose_run, description="f"))
    monkeypatch.setattr(debug_wf, "_patch_adk_node",
                        lambda: Node(id="patch", kind="tool", run=fake_patch_run, description="f"))
    hop = debug_wf.debug_hop()
    res, job, job_dir = _run_hop(hop, tmp_path, inputs="fix the add function")
    # patch.py runs but there is NO test evidence -> the gate must not SHIP
    ev = json.loads((job_dir / "EVAL.json").read_text())
    assert ev["checks"][0]["passed"] is False
    assert "no test evidence" in ev["checks"][0]["message"]
    assert res["verdict"]["verdict"] != "SHIP"


def test_build_multi_verify_no_test_evidence_never_ships(tmp_path, monkeypatch):
    """build-multi verify: main.py stub with no tests must not SHIP."""
    from nine.workflows import build_multi_wf

    def fake_build_run(inputs, job_dir):
        d = Path(job_dir) / "solution"
        d.mkdir(parents=True, exist_ok=True)
        (d / "main.py").write_text("print('TODO: implement the whole project')\n", encoding="utf-8")
        return {"output": "scaffolded solution/"}

    monkeypatch.setattr(build_multi_wf, "_build_multi_adk_node",
                        lambda: Node(id="build-multi", kind="tool", run=fake_build_run, description="f"))
    hop = build_multi_wf.build_multi_hop()
    res, job, job_dir = _run_hop(hop, tmp_path, inputs="build a project")
    ev = json.loads((job_dir / "EVAL.json").read_text())
    assert ev["checks"][0]["passed"] is False
    assert "no test evidence" in ev["checks"][0]["message"]
    assert res["verdict"]["verdict"] != "SHIP"


# ============================================================== T3-F2 review


def test_standalone_review_of_empty_workspace_fails(tmp_path):
    """review with no EVAL.json must FAIL (fabricated PASS is gone)."""
    from nine.chains.flagship import review_hop

    hop = review_hop()
    res, job, job_dir = _run_hop(hop, tmp_path, inputs="review my code")
    assert job.status == "blocked"  # FIX loop exhausted
    review_md = (job_dir / "review.md").read_text()
    assert "Verdict: FAIL" in review_md
    assert "Verdict: PASS" not in review_md
    # either honest evidence line: no EVAL.json (attempt 1) or a failed
    # check in the (self-written) EVAL.json (attempt 2+) — never a PASS
    assert ("no EVAL.json" in review_md) or ("failed check" in review_md)


def test_standalone_review_of_failing_eval_fails(tmp_path):
    """review of a real EVAL.json with failed checks must FAIL."""
    from nine.chains.flagship import review_hop

    hop = review_hop()
    res, job, job_dir = _run_hop(
        hop, tmp_path, inputs="review my code",
        seed={"EVAL.json": json.dumps({
            "checks": [{"name": "tests-pass", "passed": False, "message": "tests failed"}],
            "exit_code": 1})},
    )
    review_md = (job_dir / "review.md").read_text()
    assert "Verdict: FAIL" in review_md


# ============================================================== T3-F5 timeout


def test_node_timeout_enforced_for_callable_nodes(tmp_path):
    """A hung tool node must fail the job loud within the deadline."""
    from nine.runtime.workflows import Workflow

    def slow_run(inputs, job_dir):
        time.sleep(5)
        return {"output": "late"}

    wf = Workflow(id="slow", description="x")
    wf.add_node(Node(id="tool1", kind="tool", run=slow_run, timeout_seconds=1))
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    g = EvidenceGate()
    g.register_check("always", _always_true_check)
    ex = WorkflowExecutor(ledger, g, workdir=tmp_path / "work")
    job = ledger.submit("slow", {"task": "x"})
    start = time.monotonic()
    with pytest.raises(WorkflowError, match="exceeded timeout"):
        ex.execute(wf, job, {"task": "x"})
    assert time.monotonic() - start < 4  # did NOT wait the full 5s
    assert job.status == "failed"


def _always_true_check(ctx, workdir):
    return True, "ok"


# ============================================================== T3-F7 containment


def test_contained_write_refuses_dotdot_escape(tmp_path):
    """write_file must refuse paths resolving outside the job dir."""
    from nine.chains.flagship import _contained_write

    job_dir = tmp_path / "job"
    job_dir.mkdir()
    with pytest.raises(ValueError, match="outside job dir"):
        _contained_write(job_dir, "../ESCAPED.txt", "pwned")
    assert not (tmp_path / "ESCAPED.txt").exists()
    # absolute path also refused
    with pytest.raises(ValueError):
        _contained_write(job_dir, str(tmp_path / "abs.txt"), "x")
    # nested relative path inside the job dir works
    _contained_write(job_dir, "sub/file.txt", "ok")
    assert (job_dir / "sub" / "file.txt").read_text() == "ok"


# ============================================================== T3-F8 redaction


def test_redact_is_case_insensitive():
    from nine.router.classifier import redact

    assert "hunter2" not in redact("my PASSWORD=hunter2 and API_KEY=abc123xyz")
    assert "hunter2" not in redact("token is hunter2")
    assert "hunter2" not in redact("TOKEN: hunter2")
    # lowercase still works
    assert "hunter2" not in redact("password is hunter2")
