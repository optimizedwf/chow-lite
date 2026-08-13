"""Regression tests for the torture-harvest findings (2026-08-13).

Covers the highest-value gaps filed by the DS4 Flash torture-testers
(bench/torture/reports/torture-1.md + torture-2.md):

1. Router: unparsable model output must fall back to keywords with an
   honest model_used (never a phantom `respond` route stamped gemini).
2. Router: keyword matching is word-boundary, so "latest news" is not
   `test` and "water the plant" is not `plan`.
3. review-multi: REVIEW.md "Verdict: FAIL" must NOT pass the gate.
4. Build: no test_solution.py = UNVERIFIED (fail loud) — an exit-0 stub
   must not SHIP.
5. Review hop: verdict must be derived from EVAL.json, not hardcoded
   PASS; review.md verdict must be consistent with EVAL.json.
6. Transform: a model relabeling TARGET.txt to a junk extension must not
   smuggle unverifiable output through the gate.
7. CLI: `nine submit` returns non-zero when the verdict is not SHIP.
8. Ledger: task text is redacted at the ledger boundary.
9. CLI: cancel/recover on unknown ids print a clean error, no traceback.
"""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # noqa: E402

import os

os.environ["GEMINI_API_KEY"] = ""

from nine.gates.evidence import EvidenceGate
from nine.ledger.ledger import JSONLLedger
from nine.runtime.workflows import Node, WorkflowExecutor


def _gate(hop):
    g = EvidenceGate()
    for name, check in hop.gate_checks.items():
        g.register_check(name, check)
    return g


def _execute(hop, tmp_path, inputs=None, seed=None):
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


# ---------------------------------------------------------------- 1+2 router
class _GarbageModel:
    """Model that returns text which is NOT parseable JSON."""

    def generate_content(self, prompt):
        return SimpleNamespace(text="{{{ not json at all }}")


def _router_with(model):
    from nine.router.classifier import GeminiRouter, Router

    r = Router()
    r.register("build", ["build", "implement"])
    r.register("test", ["test", "write tests", "pytest"])
    r.register("plan", ["plan", "roadmap", "step by step"])
    r.register("respond", ["respond", "talk", "chat"])
    if model is not None:
        r.model = model
        r.model_router = GeminiRouter(model, r.workflows)
    return r


def test_router_unparsable_model_falls_back_to_keywords():
    """Parse failure = NO model decision: keyword lane + honest model_used."""
    r = _router_with(_GarbageModel())
    d = r.classify("please build a calculator")
    assert d.workflow_id == "build"          # keyword lane, not `respond`
    assert d.model == "deterministic-keyword"
    assert "unparsable" in d.reason


def test_router_unparsable_never_stamps_gemini():
    """The RouteDecision must not claim gemini routed a task it never saw."""
    r = _router_with(_GarbageModel())
    d = r.classify("write tests for the parser")
    assert d.workflow_id == "test"
    assert d.model == "deterministic-keyword"


def test_keyword_router_uses_word_boundaries():
    """Substring routing misroutes common words: latest->test, plant->plan."""
    from nine.router.classifier import KeywordRouter

    k = KeywordRouter()
    k.register("test", ["test", "write tests", "pytest"])
    k.register("plan", ["plan", "roadmap"])
    k.register("build", ["build", "implement"])
    assert k.classify("what is the latest news on AI")[0] == "respond"
    assert k.classify("the greatest advances in robotics")[0] == "respond"
    assert k.classify("water the plant in the office")[0] == "respond"
    assert k.classify("book a plane ticket to paris")[0] == "respond"
    # positive controls still route
    assert k.classify("write tests for the parser")[0] == "test"
    assert k.classify("please build a calculator")[0] == "build"
    assert k.classify("make a roadmap for q3")[0] == "plan"


# ------------------------------------------------------------ 3 review-multi
def test_review_multi_fail_verdict_blocks(tmp_path):
    """A REVIEW.md that says FAIL must never pass the review-multi gate."""
    from nine.workflows.review_multi_wf import _review_verdict_check

    (tmp_path / "REVIEW.md").write_text(
        "## Overall Verdict: FAIL — sql injection everywhere\n",
        encoding="utf-8",
    )
    ok, msg = _review_verdict_check({}, tmp_path)
    assert ok is False
    assert "PASS" in msg

    (tmp_path / "REVIEW.md").write_text(
        "## Overall Verdict: PASS\n", encoding="utf-8")
    ok, _ = _review_verdict_check({}, tmp_path)
    assert ok is True


# ------------------------------------------------------------ 4 build no tests
def test_build_without_tests_fails_loud(tmp_path, monkeypatch):
    """A solution.py with no test_solution.py is UNVERIFIED, never SHIP."""
    from nine.chains import flagship

    def fake_build(inputs, job_dir):
        (Path(job_dir) / "solution.py").write_text(
            "def add(a, b):\n    return a + b\n", encoding="utf-8")
        return {"output": "wrote solution.py only (no tests)"}

    monkeypatch.setattr(flagship, "_build_adk_node",
                        lambda: Node(id="build", kind="tool", run=fake_build))
    hop = flagship.build_hop()
    res, job, jd = _execute(hop, tmp_path, inputs="make add work")
    ev = json.loads((jd / "EVAL.json").read_text())
    assert ev["checks"][0]["passed"] is False
    assert "no test evidence" in ev["checks"][0]["message"]
    assert res["verdict"]["verdict"] != "SHIP"


# ------------------------------------------------------------ 5 review hop
def test_review_hop_derives_fail_from_eval(tmp_path):
    """Failing EVAL.json must produce a FAIL review, not a rubber-stamp PASS."""
    from nine.chains.flagship import review_hop

    hop = review_hop()
    seed = {"EVAL.json": json.dumps(
        {"checks": [{"name": "tests-pass", "passed": False,
                     "message": "2 failed"}], "exit_code": 1})}
    res, job, jd = _execute(hop, tmp_path, inputs="review it", seed=seed)
    review = (jd / "review.md").read_text()
    assert "Verdict: FAIL" in review
    assert res["verdict"]["verdict"] != "SHIP"


def test_review_hop_passes_on_clean_eval(tmp_path):
    """Passing EVAL.json still produces a PASS review (no regression)."""
    from nine.chains.flagship import review_hop

    hop = review_hop()
    seed = {"EVAL.json": json.dumps(
        {"checks": [{"name": "tests-pass", "passed": True,
                     "message": "3 passed"}], "exit_code": 0})}
    res, job, jd = _execute(hop, tmp_path, inputs="review it", seed=seed)
    review = (jd / "review.md").read_text()
    assert "Verdict: PASS" in review
    assert res["verdict"]["verdict"] == "SHIP"


def test_review_verdict_must_match_eval(tmp_path):
    """review.md claiming PASS over a failing EVAL.json is a gate violation."""
    from nine.chains.flagship import _review_verdict_consistent

    (tmp_path / "EVAL.json").write_text(json.dumps(
        {"checks": [{"name": "x", "passed": False}], "exit_code": 1}),
        encoding="utf-8")
    (tmp_path / "review.md").write_text("# Review\nVerdict: PASS\n",
                                        encoding="utf-8")
    ok, msg = _review_verdict_consistent({}, tmp_path)
    assert ok is False
    assert "says PASS" in msg and "failing" in msg

    (tmp_path / "review.md").write_text("# Review\nVerdict: FAIL\n",
                                        encoding="utf-8")
    ok, _ = _review_verdict_consistent({}, tmp_path)
    assert ok is True


# ------------------------------------------------------------ 6 transform
def test_transform_rejects_unverifiable_target(tmp_path, monkeypatch):
    """TARGET.txt relabeled to a junk extension must not smuggle output."""
    from nine.workflows import transform_wf

    def noop_transform(inputs, job_dir):
        # simulate the malicious model: relabel TARGET.txt to a junk
        # extension and write unverifiable text output
        (Path(job_dir) / "TARGET.txt").write_text("txt", encoding="utf-8")
        (Path(job_dir) / "OUTPUT.txt").write_text(
            "blah blah blah not json", encoding="utf-8")
        return {"output": "noop (hermetic)"}

    monkeypatch.setattr(
        transform_wf, "_transform_tool_node",
        lambda: Node(id="transform", kind="tool", run=noop_transform))
    hop = transform_wf.transform_hop()
    seed = {}
    res, job, jd = _execute(hop, tmp_path, inputs="convert this csv to json",
                            seed=seed)
    ev = json.loads((jd / "EVAL.json").read_text())
    assert ev["checks"][0]["passed"] is False
    assert "unsupported target format" in ev["checks"][0]["message"]
    assert res["verdict"]["verdict"] != "SHIP"


def test_transform_still_ships_valid_json(tmp_path, monkeypatch):
    """Legit json output still passes (no regression)."""
    from nine.workflows import transform_wf

    def noop_transform(inputs, job_dir):
        return {"output": "noop (hermetic)"}

    monkeypatch.setattr(
        transform_wf, "_transform_tool_node",
        lambda: Node(id="transform", kind="tool", run=noop_transform))
    hop = transform_wf.transform_hop()
    seed = {"TARGET.txt": "json",
            "OUTPUT.json": json.dumps({"rows": [{"a": 1}]})}
    res, job, jd = _execute(hop, tmp_path, inputs="convert this csv to json",
                            seed=seed)
    ev = json.loads((jd / "EVAL.json").read_text())
    assert ev["checks"][0]["passed"] is True
    assert res["verdict"]["verdict"] == "SHIP"


# ------------------------------------------------------------ 7 submit exit
def test_cmd_submit_returns_2_on_nonship(tmp_path, monkeypatch):
    """A non-SHIP verdict must surface as a non-zero exit code."""
    from nine import cli as nine_cli
    from nine.registry import WORKFLOWS
    from nine.runtime.workflows import Workflow

    wf = Workflow(id="blockme")
    wf.add_node(Node(id="noop", kind="bash", command="echo hi > note.txt"))
    monkeypatch.setitem(WORKFLOWS, "blockme", lambda: wf)
    monkeypatch.setattr(
        nine_cli, "build_default_router",
        lambda: SimpleNamespace(classify=lambda t: SimpleNamespace(
            workflow_id="blockme", to_dict=lambda: {})))
    monkeypatch.setattr(nine_cli, "_record_route_event", lambda *a, **k: None)
    monkeypatch.setattr(nine_cli, "_learner", lambda args: None)
    args = SimpleNamespace(task="x", ledger=str(tmp_path / "l.jsonl"),
                           workdir=str(tmp_path),
                           events=str(tmp_path / "e.jsonl"))
    assert nine_cli.cmd_submit(args) == 2


# ------------------------------------------------------------ 8 redaction
def test_cmd_submit_redacts_task_in_ledger(tmp_path, monkeypatch):
    """The ledger must never persist raw credential-shaped task text."""
    from nine import cli as nine_cli

    captured = {}

    class _StubLedger:
        def submit(self, workflow_id, input):
            captured["input"] = input
            return SimpleNamespace(
                job_id="j1", status="queued", workflow_id=workflow_id,
                attach_route_decision=lambda d: None)

        def update(self, job):
            pass

    monkeypatch.setattr(nine_cli, "_ledger", lambda args: _StubLedger())
    monkeypatch.setattr(
        nine_cli, "build_default_router",
        lambda: SimpleNamespace(classify=lambda t: SimpleNamespace(
            workflow_id="respond", to_dict=lambda: {})))
    monkeypatch.setattr(nine_cli, "_record_route_event", lambda *a, **k: None)
    monkeypatch.setattr(nine_cli, "_learner", lambda args: None)
    monkeypatch.setattr(
        nine_cli.WorkflowExecutor, "execute",
        lambda self, wf, job, inputs: {"verdict": {"verdict": "SHIP",
                                                   "summary": "ok"}})
    args = SimpleNamespace(
        task="my password is hunter2 and token is sk-ABCDEF1234567890",
        ledger=str(tmp_path / "l.jsonl"), workdir=str(tmp_path),
        events=str(tmp_path / "e.jsonl"))
    assert nine_cli.cmd_submit(args) == 0
    stored = str(captured["input"])
    assert "hunter2" not in stored
    assert "sk-ABCDEF" not in stored
    assert "password=***" in stored


# ------------------------------------------------------------ 9 cancel/recover
def test_cmd_cancel_recover_unknown_id_clean(tmp_path, capsys):
    """Unknown job ids must yield a one-line error, never a traceback."""
    from nine.cli import cmd_cancel, cmd_recover

    args = SimpleNamespace(job_id="nope", ledger=str(tmp_path / "l.jsonl"),
                           workdir=str(tmp_path),
                           events=str(tmp_path / "e.jsonl"))
    assert cmd_cancel(args) == 1
    assert cmd_recover(args) == 1
    err = capsys.readouterr().err
    assert "Traceback" not in err
    assert "job not found" in err


# ============================================================ HARVEST 2 (2026-08-13)
# T2-F1/T1-F8 research+plan hops are model-driven (no canned stubs)
# T1-F5/T2-F4 chain manifest: no cross-hop misattribution
# T1-F7 nine recover RE-EXECUTES (no dead-end status)
# T2-F8 summarize-standalone never SHIPs a "summary of nothing"

def test_research_hop_fails_loud_without_model(tmp_path):
    """Research must be model-driven: no key = loud WorkflowError, and NO
    canned research.md is ever written (T1-F8/T2-F1 — was a bash stub that
    stamped 'Key insight: evidence-gated execution keeps agents honest.')."""
    from nine.chains.flagship import research_hop
    from nine.runtime.workflows import WorkflowError

    hop = research_hop()
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate=_gate(hop), workdir=tmp_path / "work")
    job = ledger.submit("research", {"task": "study black holes"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(WorkflowError):
        ex.execute(hop.workflow, job, {"task": "study black holes"})
    assert job.status == "failed"
    assert not (job_dir / "research.md").exists()
    assert not (job_dir / "HANDOFF.md").exists()


def test_plan_hop_fails_loud_without_model(tmp_path):
    """Plan must be model-driven: no key = loud WorkflowError, no canned
    PLAN.md template (T2-F1)."""
    from nine.chains.flagship import plan_hop
    from nine.runtime.workflows import WorkflowError

    hop = plan_hop()
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate=_gate(hop), workdir=tmp_path / "work")
    job = ledger.submit("plan", {"task": "build a calculator"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    with pytest.raises(WorkflowError):
        ex.execute(hop.workflow, job, {"task": "build a calculator"})
    assert job.status == "failed"
    assert not (job_dir / "PLAN.md").exists()


def test_research_hop_ships_fake_findings(monkeypatch, tmp_path):
    """With a model (faked), research writes REAL findings and SHIPs."""
    from nine.chains import flagship
    from nine.runtime import summarizer

    monkeypatch.setattr(
        summarizer, "summarize_text",
        lambda text, max_words=120, task="", api_key=None:
        ("distilled research about black holes", "fake-gemini"),
    )

    def fake_research_run(inputs, job_dir):
        (Path(job_dir) / "research.md").write_text(
            "# Findings\n\nBlack holes emit Hawking radiation; this task "
            "needs a step-by-step evidence check.\n", encoding="utf-8")
        return {"output": "wrote research.md"}

    monkeypatch.setattr(
        flagship, "_research_adk_node",
        lambda: Node(id="research", kind="tool", run=fake_research_run,
                     description="fake research node (hermetic)"),
    )
    res, job, job_dir = _execute(flagship.research_hop(), tmp_path,
                                 inputs="study black holes")
    assert res["verdict"]["verdict"] == "SHIP"
    text = (job_dir / "research.md").read_text()
    assert "Hawking radiation" in text
    assert "Key insight: evidence-gated" not in text  # old canned stub gone


def test_plan_hop_ships_real_plan(monkeypatch, tmp_path):
    """With a model (faked), plan writes a task-specific PLAN.md and SHIPs."""
    from nine.chains import flagship

    def fake_plan_run(inputs, job_dir):
        (Path(job_dir) / "PLAN.md").write_text(
            "# Plan\n\n1. scaffold\n2. implement\n3. verify with EVAL.json\n",
            encoding="utf-8")
        return {"output": "wrote PLAN.md"}

    monkeypatch.setattr(
        flagship, "_plan_adk_node",
        lambda: Node(id="plan", kind="tool", run=fake_plan_run,
                     description="fake plan node (hermetic)"),
    )
    res, job, job_dir = _execute(flagship.plan_hop(), tmp_path,
                                 inputs="build a calculator",
                                 seed={"HANDOFF.md": "distilled research about the calculator\n"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert (job_dir / "PLAN.md").exists()
    assert "1. scaffold" in (job_dir / "PLAN.md").read_text()


def test_chain_manifest_no_cross_hop_misattribution(tmp_path, monkeypatch):
    """Hop manifests must NOT re-register earlier hops' untouched files:
    each hop's ledger view = only what THAT hop produced (T1-F5/T2-F4)."""
    from nine.chains import flagship
    from nine.chains.chain import Chain, ChainExecutor

    def fake_build_run(inputs, job_dir):
        (Path(job_dir) / "solution.py").write_text(
            "def answer():\n    return 42\n", encoding="utf-8")
        (Path(job_dir) / "test_solution.py").write_text(
            "from solution import answer\ndef test_answer():\n    assert answer() == 42\n",
            encoding="utf-8")
        return {"output": "wrote solution.py + test_solution.py"}

    monkeypatch.setattr(
        flagship, "_build_adk_node",
        lambda: Node(id="build", kind="tool", run=fake_build_run,
                     description="fake build node (hermetic)"),
    )
    chain = Chain(id="t", hops=[flagship.build_hop(), flagship.review_hop()])
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = ChainExecutor(ledger, workdir=tmp_path / "work")
    job = ledger.submit("t", {"task": "build a tiny thing"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text("build a tiny thing\n")
    res = ex.execute(chain, job, {"task": "build a tiny thing"})
    assert res["final"] == "SHIPPED"

    # chain roll-up: solution.py appears exactly ONCE (from the build hop)
    chain_arts = ledger.get(job.job_id).artifacts
    sols = [a for a in chain_arts if a["name"] == "solution.py"]
    assert len(sols) == 1

    # the review hop's OWN manifest must not claim build-hop files
    review_jobs = [j for j in ledger.discover() if j.workflow_id == "t::review"]
    assert review_jobs
    review_names = {a["name"] for a in review_jobs[0].artifacts}
    assert review_names <= {"review.md", "EVAL.json"}
    build_jobs = [j for j in ledger.discover() if j.workflow_id == "t::build"]
    assert build_jobs
    build_names = {a["name"] for a in build_jobs[0].artifacts}
    assert {"solution.py", "test_solution.py"} <= build_names


def test_recover_reexecutes_blocked_job(tmp_path, monkeypatch):
    """nine recover must RE-EXECUTE a blocked job (fresh evidence), not
    park it in a dead-end status (T1-F7)."""
    from nine import cli as nine_cli
    from nine.router.classifier import RouteDecision
    from nine.runtime import responder

    monkeypatch.setattr(
        responder, "respond_text",
        lambda task, max_chars=600: ("a real model answer", "gemini"),
    )

    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    dec = RouteDecision(
        decision_id="d1", task_redacted="zzz qqq recover me", workflow_id="respond",
        confidence=0.9, reason="fallback", decided_at="now", router_version="0.1.0",
    )
    job = ledger.submit("respond", {"task": dec.task_redacted})
    job.attach_route_decision(dec)
    job.status = "blocked"  # simulate a previous failed run
    ledger.update(job)

    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True)
    (job_dir / "task.txt").write_text("zzz qqq recover me\n")
    (job_dir / "stale.md").write_text("old garbage from failed attempt\n")

    args = SimpleNamespace(job_id=job.job_id,
                           ledger=str(tmp_path / "ledger.jsonl"),
                           workdir=str(tmp_path / "work"),
                           events=str(tmp_path / "e.jsonl"))
    assert nine_cli.cmd_recover(args) == 0
    # cmd_recover runs on a freshly re-opened ledger: assert the PERSISTED
    # state, not the stale in-memory object
    fresh = JSONLLedger(tmp_path / "ledger.jsonl")
    assert fresh.get(job.job_id).status == "shipped"
    assert not (job_dir / "stale.md").exists()  # stale attempt wiped
    assert (job_dir / "task.txt").read_text().strip() == "zzz qqq recover me"
    assert (job_dir / "RESPONSE.md").exists()


def test_summarize_standalone_empty_workspace_blocks(tmp_path, monkeypatch):
    """A 'summary of nothing' (no source files in the workspace) must NEVER
    SHIP — the lane exists to distill real source (T2-F8)."""
    from nine.runtime import summarizer
    from nine.workflows.summarize_standalone_wf import summarize_standalone_hop

    monkeypatch.setattr(
        summarizer, "summarize_text",
        lambda text, max_words=120, task="", api_key=None:
        ("nothing to summarize", "fake-gemini"),
    )
    hop = summarize_standalone_hop()
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate=_gate(hop), workdir=tmp_path / "work")
    job = ledger.submit("summarize-standalone", {"task": "summarize the code"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    res = ex.execute(hop.workflow, job, {"task": "summarize the code"})
    # FIX loop exhausted -> job BLOCKED (never SHIP); the returned verdict
    # is the last gate FIX, so assert on the terminal ledger state too.
    assert job.status == "blocked"
    src_check = res["verdict"]["eval_results"]["source-present"]
    assert src_check["passed"] is False
    assert "no source files" in src_check["message"]


def test_summarize_standalone_ships_with_source(tmp_path, monkeypatch):
    """With real source present, summarize-standalone SHIPs a real summary."""
    from nine.runtime import summarizer
    from nine.workflows.summarize_standalone_wf import summarize_standalone_hop

    monkeypatch.setattr(
        summarizer, "summarize_text",
        lambda text, max_words=120, task="", api_key=None:
        ("the module computes fibonacci", "fake-gemini"),
    )
    hop = summarize_standalone_hop()
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    ex = WorkflowExecutor(ledger, gate=_gate(hop), workdir=tmp_path / "work")
    job = ledger.submit("summarize-standalone", {"task": "summarize the code"})
    job_dir = tmp_path / "work" / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "solution.py").write_text(
        "def fib(n):\n    return n if n < 2 else fib(n-1) + fib(n-2)\n",
        encoding="utf-8")
    res = ex.execute(hop.workflow, job, {"task": "summarize the code"})
    assert res["verdict"]["verdict"] == "SHIP"
    assert (job_dir / "SUMMARY.md").exists()
    assert "fibonacci" in (job_dir / "SUMMARY.md").read_text()
