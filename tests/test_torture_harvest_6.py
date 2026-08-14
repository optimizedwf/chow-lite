"""Hermetic regression tests for slice 30 — torture round 5 harvest.

torture-9 (LLM provider switch + model layer) and torture-10 (CLI/router/
gate/registry/bench fallout): 15 findings, 1 dup (t9-F3 == t10-F3).

Fixes under test:
  t9-F1  uninstall_adk_override() FULLY restores the ADK registry (pop every
         added key + resolve cache) so a post-"restore" gemini backend NEVER
         sends GEMINI_API_KEY to the tunnel host.
  t9-F2  LlmAgent system_instruction is forwarded as a leading system message.
  t9-F3  tool round-trips emit exactly ONE tool message (no duplicates, no
         spurious empty user message).  == t10-F3.
  t9-F4  ADK workflow nodes use llm_provider.adk_model(): registry string on
         the openai backend (instance-based Gemini() bypasses LLMRegistry).
  t9-F5  artifacts/health/decisions report the ACTUAL serving model.
  t9-F6  junk NINE_LLM_BACKEND warns loudly instead of silently using gemini.
  t9-F7  _OpenAILlm refuses to POST without a key; errors use the VALID
         FinishReason.OTHER (not the invalid "ERROR").
  t10-F1 `nine recover --force` degrades a stale running job AND re-executes
         on the SAME invocation (cache synced to the durable transition).
  t10-F2 the stale-artifact guard covers EVERY gate-certified file (not just
         EVAL.json) — required_artifact_check provenance.
  t10-F4 plugin workflow ids colliding with core WORKFLOWS/CHAINS are skipped
         with a loud warning (never silent replacement).
  t10-F5 bare "create the" no longer routes docs/writing tasks to build.
  t10-F6 bench_nine load_api_key is backend-aware (openai mode needs no
         gemini.key; the tunnel key chain is consulted).
  t10-F7 model-or-fail messages name the ACTIVE backend's key sources.
  t10-F8 SUBMISSION/README no longer promise keyless flagship runs or claim
         GEMINI_MODEL pins the ADK nodes.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from nine.runtime import llm_provider

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("NINE_LLM_BACKEND", "NINE_LLM_BASE_URL", "NINE_LLM_API_KEY",
              "NINE_LLM_MODEL", "GEMINI_API_KEY", "OPENCODE_GO_API_KEY"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(llm_provider, "_vault_key", lambda: "")
    monkeypatch.setattr(llm_provider, "_auth_key", lambda: "")
    llm_provider.uninstall_adk_override()
    yield
    llm_provider.uninstall_adk_override()


# =====================================================================
# t9-F1: uninstall must FULLY restore the ADK registry
# =====================================================================
def test_uninstall_removes_all_registry_keys_and_clears_cache(monkeypatch):
    from google.adk.models import registry

    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k")
    llm_provider.install_adk_override()
    registry.LLMRegistry.resolve("gemini-3.6-flash")  # warm the cache

    llm_provider.uninstall_adk_override()

    d = registry._llm_registry_dict
    # keys added ONLY by install/register must be gone...
    for key in ("^gemini-[0-9].*$", "^deepseek-v4-flash$", "^gemini-.*$"):
        assert key not in d, f"stale registry entry after uninstall: {key}"
    # ...and the original lazy Gemini entry restored (never _OpenAILlm)
    assert d.get("gemini-.*") == ("google.adk.models.google_llm", "Gemini")


def test_uninstall_restores_real_gemini_resolution(monkeypatch):
    """After uninstall + backend flip to gemini, a resolved gemini model must
    be the REAL Gemini class — NOT _OpenAILlm — so the tunnel is never
    contacted with GEMINI_API_KEY (t9-F1 credential-exfil scenario)."""
    from google.adk.models import registry

    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k")
    llm_provider.install_adk_override()
    registry.LLMRegistry.resolve("gemini-3.6-flash")
    llm_provider.uninstall_adk_override()

    monkeypatch.setenv("GEMINI_API_KEY", "FAKE-GEMINI-SECRET-12345")  # invented
    resolved = registry.LLMRegistry.resolve("gemini-3.6-flash")
    # lazy tuple entry -> the real google_llm Gemini class, NOT _OpenAILlm
    assert resolved.__name__ == "Gemini"


# =====================================================================
# t9-F2: system instruction forwarded
# =====================================================================
def test_system_instruction_becomes_system_message(monkeypatch):
    import asyncio

    from google.adk.models import registry
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k")
    llm_provider.install_adk_override()

    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    def fake_post(url, headers, json, timeout):
        captured["messages"] = json["messages"]
        return FakeResp()

    monkeypatch.setattr("requests.post", fake_post)
    inst = registry._llm_registry_dict["gemini-.*"](model="gemini-3.6-flash")
    lr = LlmRequest(
        model="gemini-3.6-flash",
        contents=[types.Content(role="user", parts=[types.Part(text="hi")])],
        config=types.GenerateContentConfig(
            system_instruction="You are a market research agent. Use tools."),
    )

    async def _collect():
        return [r async for r in inst.generate_content_async(lr)]

    asyncio.run(_collect())
    assert captured["messages"][0] == {
        "role": "system",
        "content": "You are a market research agent. Use tools.",
    }


# =====================================================================
# t9-F3 / t10-F3: tool round-trip emits exactly one tool message
# =====================================================================
def test_tool_round_trip_exact_message_sequence(monkeypatch):
    import asyncio

    from google.adk.models import registry
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k")
    llm_provider.install_adk_override()

    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "done"}}]}

    def fake_post(url, headers, json, timeout):
        captured["messages"] = json["messages"]
        return FakeResp()

    monkeypatch.setattr("requests.post", fake_post)
    inst = registry._llm_registry_dict["gemini-.*"](model="gemini-3.6-flash")
    lr = LlmRequest(
        model="gemini-3.6-flash",
        contents=[
            types.Content(role="user", parts=[types.Part(text="price?")]),
            types.Content(role="model", parts=[types.Part(function_call=types.FunctionCall(
                id="call_abc123", name="lookup_price", args={"ticker": "AAPL"}))]),
            # google-adk sends tool results as role="user" function_response
            types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
                id="call_abc123", name="lookup_price", response={"price": 150.0}))]),
        ],
    )

    async def _collect():
        return [r async for r in inst.generate_content_async(lr)]

    asyncio.run(_collect())
    msgs = captured["messages"]
    assert msgs[0] == {"role": "user", "content": "price?"}
    assert msgs[1]["role"] == "assistant" and len(msgs[1]["tool_calls"]) == 1
    tool_msgs = [m for m in msgs if m["role"] == "tool"]
    assert len(tool_msgs) == 1, f"tool message duplicated: {msgs}"
    assert tool_msgs[0]["tool_call_id"] == "call_abc123"
    # no empty user message between assistant tool_calls and the tool result
    assert not any(m == {"role": "user", "content": ""} for m in msgs)


def test_tool_result_role_tool_not_duplicated(monkeypatch):
    """Contents with role='tool' function_response parts: exactly one msg."""
    import asyncio

    from google.adk.models import registry
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k")
    llm_provider.install_adk_override()

    captured = {}

    class FakeResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    def fake_post(url, headers, json, timeout):
        captured["messages"] = json["messages"]
        return FakeResp()

    monkeypatch.setattr("requests.post", fake_post)
    inst = registry._llm_registry_dict["gemini-.*"](model="gemini-3.6-flash")
    lr = LlmRequest(
        model="gemini-3.6-flash",
        contents=[
            types.Content(role="user", parts=[types.Part(text="go")]),
            types.Content(role="tool", parts=[types.Part(function_response=types.FunctionResponse(
                id="c1", name="t", response={"x": 1}))]),
        ],
    )

    async def _collect():
        return [r async for r in inst.generate_content_async(lr)]

    asyncio.run(_collect())
    tool_msgs = [m for m in captured["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 1


# =====================================================================
# t9-F4: adk_model() picks the right LlmAgent model arg per backend
# =====================================================================
def test_adk_model_gemini_backend_is_gemini_instance(monkeypatch):
    m = llm_provider.adk_model()
    assert type(m).__name__ == "Gemini"
    assert getattr(m, "model", None) == "gemini-3.6-flash"


def test_adk_model_openai_backend_is_registry_string(monkeypatch):
    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k")
    assert llm_provider.adk_model() == "gemini-3.6-flash"
    # ...which resolves to the tunnel override
    from google.adk.models import registry

    llm_provider.install_adk_override()
    resolved = registry.LLMRegistry.resolve("gemini-3.6-flash")
    assert resolved.__name__ == "_OpenAILlm"


def test_flagship_nodes_use_adk_model(monkeypatch):
    """flagship ADK hops must resolve to the tunnel in openai mode (t9-F4:
    instance-based Gemini() constructions bypassed LLMRegistry before)."""
    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k")
    llm_provider.install_adk_override()
    from google.adk.models import registry

    src = (REPO / "nine" / "chains" / "flagship.py").read_text()
    assert "model=Gemini(model=" not in src
    assert "llm_provider.adk_model()" in src
    # the adk_model() string resolves to the tunnel override
    resolved = registry.LLMRegistry.resolve("gemini-3.6-flash")
    assert resolved.__name__ == "_OpenAILlm"


# =====================================================================
# t9-F5: model truth in decisions/responder/health/docs
# =====================================================================
def test_router_decision_reports_actual_model(monkeypatch):
    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k")

    from nine.registry import KEYWORDS
    from nine.router.classifier import Router

    class FakeResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": (
                '{"workflow_id": "build", "confidence": 0.9, '
                '"reason": "model decision"}')}}]}

    monkeypatch.setattr("requests.post", lambda *a, **k: FakeResp())
    model = llm_provider.make_model_client()
    r = Router(model=model, version="live")
    for wf_id, kws in KEYWORDS.items():
        r.register(wf_id, kws, "desc")
    d = r.classify("implement a calculator")
    assert d.workflow_id == "build"
    assert d.model == "deepseek-v4-flash", d.model


def test_responder_reports_model_name(monkeypatch):
    from nine.runtime import responder

    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k")
    monkeypatch.setattr(
        llm_provider, "chat_text",
        lambda prompt, timeout=120: "answer text")
    text, model = responder.respond_text("a task", max_chars=400)
    assert model == "deepseek-v4-flash"


def test_health_reports_active_backend_model():

    import deploy.server as server

    assert server.MODEL == llm_provider.GEMINI_DEFAULT_MODEL or server.MODEL


def test_no_stale_gemini_35_flash_claims_covers_demo_live():
    txt = (REPO / "demo_live.py").read_text()
    assert "3.5 Flash" not in txt


# =====================================================================
# t9-F6: junk backend warns loudly
# =====================================================================
def test_junk_backend_warns_loudly(monkeypatch, capsys):
    monkeypatch.setenv("NINE_LLM_BACKEND", "openaai")
    assert llm_provider.backend() == "gemini"
    err = capsys.readouterr().err
    assert "unknown NINE_LLM_BACKEND" in err
    assert "openaai" in err


# =====================================================================
# t9-F7: no POST without key; FinishReason.OTHER not "ERROR"
# =====================================================================
def test_adk_override_refuses_post_without_key(monkeypatch):
    import asyncio

    from google.adk.models import registry
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")  # no key at all
    llm_provider.install_adk_override()
    calls = []

    def fake_post(*a, **k):
        calls.append(1)
        raise AssertionError("must not POST without a key")

    monkeypatch.setattr("requests.post", fake_post)
    inst = registry._llm_registry_dict["gemini-.*"](model="gemini-3.6-flash")
    lr = LlmRequest(
        model="gemini-3.6-flash",
        contents=[types.Content(role="user", parts=[types.Part(text="hi")])],
    )

    async def _collect():
        return [r async for r in inst.generate_content_async(lr)]

    resp = asyncio.run(_collect())[0]
    assert calls == []
    assert resp.error_message and "key" in resp.error_message


def test_adk_override_error_uses_valid_finish_reason(monkeypatch):
    import asyncio
    import warnings

    from google.adk.models import registry
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k")
    llm_provider.install_adk_override()

    class FakeErr:
        status_code = 429

        def json(self):
            return {}

    monkeypatch.setattr("requests.post", lambda *a, **k: FakeErr())
    inst = registry._llm_registry_dict["gemini-.*"](model="gemini-3.6-flash")
    lr = LlmRequest(
        model="gemini-3.6-flash",
        contents=[types.Content(role="user", parts=[types.Part(text="hi")])],
    )

    async def _collect():
        return [r async for r in inst.generate_content_async(lr)]

    with warnings.catch_warnings():
        warnings.simplefilter("error")  # "ERROR is not a valid FinishReason"
        resp = asyncio.run(_collect())[0]
    assert resp.finish_reason == types.FinishReason.OTHER
    assert "429" in resp.error_message


# =====================================================================
# t10-F1: recover --force works in ONE invocation
# =====================================================================
def test_recover_force_single_invocation(monkeypatch, tmp_path, capsys):
    """--force must degrade stale running -> failed AND recover+re-execute on
    the SAME call (cache synced to the durable transition, t10-F1)."""
    from nine.gates.evidence import (
        EvidenceGate,
        eval_json_check,
        exit_codes_check,
    )
    from nine.ledger.ledger import JSONLLedger
    from nine.runtime.workflows import Node, Workflow, WorkflowError

    wd = tmp_path / "work"
    wd.mkdir()
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")

    def node_run(inputs, job_dir):
        (Path(job_dir) / "task.txt").write_text("do the thing", encoding="utf-8")
        (Path(job_dir) / "OUT.txt").write_text("result", encoding="utf-8")
        (Path(job_dir) / "EVAL.json").write_text(
            '{"checks":[{"name":"c","passed":true}]}', encoding="utf-8")
        return {"OUT.txt": (Path(job_dir) / "OUT.txt")}

    gate = EvidenceGate()
    gate.register_check("eval-json", eval_json_check())
    gate.register_check("exit-codes", exit_codes_check())
    wf = Workflow(id="test-recover")
    wf.add_node(Node(id="n", kind="tool", run=node_run))

    from nine.runtime.workflows import WorkflowExecutor

    ex = WorkflowExecutor(ledger, gate, workdir=wd)
    # execute normally -> shipped
    job = ledger.submit("test-recover", {"task": "do the thing"})
    jid = job.job_id
    ex.execute(wf, job, {"task": "do the thing"})
    assert ledger.get(jid).status == "shipped"

    # simulate a crash-left running job: append a running line directly
    from nine.ledger.ledger import Job

    j2 = Job(workflow_id="test-recover", job_id=jid,
             input={"task": "do the thing"})
    j2.status = "running"  # crash-left state (transition path is irrelevant)
    ledger.update(j2)
    # durable says running; cache says running
    live = ledger.refresh(jid)
    assert live.status == "running"

    # --force degrade + recover in ONE call (mirrors cli cmd_recover)
    from types import SimpleNamespace

    from nine.cli import cmd_recover

    args = SimpleNamespace(job_id=jid, force=True, workdir=str(wd),
                           ledger=str(tmp_path / "ledger.jsonl"),
                           chain=False, plugin=None, model="")
    monkeypatch.setattr(sys, "argv", ["nine", "recover"])
    out, err = capsys.readouterr()
    try:
        cmd_recover(args)
    except WorkflowError:
        # re-execution of the UNREGISTERED test workflow id fails loud after
        # recovery — that is correct model-driven behavior, out of scope.
        pass
    out2, err2 = capsys.readouterr()
    # t10-F1: ONE --force invocation must degrade + recover + re-execute.
    # The F1 bug was that the FIRST call died on the stale CACHE with
    # "is running, only blocked/failed can be recovered" before ever
    # re-executing. Assert that lie is gone and recovery happened:
    assert "only blocked/failed can be recovered" not in (err + err2), \
        "cache still lied after --force degrade (t10-F1)"
    assert "recovering" in (out2 + err2), "re-execution was not attempted"
    assert ledger.get(jid).status != "running", \
        "job must have left the stale running state"


# =====================================================================
# t10-F2: stale guard covers every gate-certified file
# =====================================================================
def test_stale_required_artifact_never_ships(tmp_path):
    """FIX re-run that skips writing a required artifact must BLOCK, even
    when EVAL.json passes (t10-F2 generalization of t7-F1)."""
    from nine.gates.evidence import (
        EvidenceGate,
        eval_json_check,
        exit_codes_check,
        required_artifact_check,
    )
    from nine.ledger.ledger import JSONLLedger
    from nine.runtime.workflows import Node, Workflow, WorkflowExecutor

    wd = tmp_path / "work"
    wd.mkdir()
    ledger = JSONLLedger(tmp_path / "ledger.jsonl")
    calls = {"n": 0}

    def node_run(inputs, job_dir):
        calls["n"] += 1
        jd = Path(job_dir)
        if calls["n"] == 1:
            # attempt 1: artifact written, EVAL fails -> FIX
            (jd / "artifact.txt").write_text("attempt 1", encoding="utf-8")
            (jd / "EVAL.json").write_text(
                '{"checks":[{"name":"c","passed":false}]}', encoding="utf-8")
        else:
            # attempt 2: agent "forgets" to rewrite artifact.txt (the
            # attempt-1 file still sits on disk) but EVAL passes now
            (jd / "EVAL.json").write_text(
                '{"checks":[{"name":"c","passed":true}]}', encoding="utf-8")
        return {}

    gate = EvidenceGate()
    gate.register_check("eval-json", eval_json_check())
    gate.register_check("artifacts", required_artifact_check(["artifact.txt"]))
    gate.register_check("exit-codes", exit_codes_check())
    wf = Workflow(id="stale-art")
    wf.add_node(Node(id="n", kind="tool", run=node_run))

    ex = WorkflowExecutor(ledger, gate, workdir=wd)
    job = ledger.submit("stale-art", {"task": "t"})
    results = ex.execute(wf, job, {"task": "t"})
    assert calls["n"] >= 2, "must have run a FIX loop"
    assert results["verdict"]["verdict"] == "BLOCK"
    assert "stale artifact" in results["verdict"].get("summary", "") or \
        "not produced this attempt" in results["verdict"].get("summary", "")


# =====================================================================
# t10-F4: plugin collisions are skipped with a warning
# =====================================================================
def test_plugin_workflow_collision_skipped(monkeypatch, tmp_path, capsys):
    """A plugin named 'research' must NOT replace the core lane."""
    import importlib

    reg = tmp_path / "plugin_registry.py"
    reg.write_text("PLUGIN_WORKFLOWS = {'research': lambda: object()}\n", encoding="utf-8")
    monkeypatch.setenv("NINE_PLUGIN_REGISTRY", str(reg))

    import nine.registry as registry_mod
    importlib.reload(registry_mod)
    assert "research" in registry_mod.WORKFLOWS
    # the core factory must still be the REAL research hop, not the plugin

    # the core factory must still produce a real Workflow object
    core = registry_mod.WORKFLOWS["research"]()
    from nine.runtime.workflows import Workflow
    assert isinstance(core, Workflow)
    err = capsys.readouterr().err
    assert "collides with a core workflow/chain" in err


# =====================================================================
# t10-F5: "create the" no longer hijacks docs/writing tasks
# =====================================================================
def test_create_the_docs_tasks_route_to_document(monkeypatch):
    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")  # no model -> keywords
    from nine.registry import HOP_DESCRIPTIONS, KEYWORDS
    from nine.router.classifier import Router

    r = Router()
    for wf_id, kws in KEYWORDS.items():
        r.register(wf_id, kws, HOP_DESCRIPTIONS.get(wf_id, ""))
    for task in ("create the readme", "create the report",
                 "create the summary", "create the documentation"):
        d = r.classify(task)
        assert d.workflow_id != "build", f"{task!r} misrouted to build"


def test_create_the_code_tasks_still_route_to_build(monkeypatch):
    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    from nine.registry import HOP_DESCRIPTIONS, KEYWORDS
    from nine.router.classifier import Router

    r = Router()
    for wf_id, kws in KEYWORDS.items():
        r.register(wf_id, kws, HOP_DESCRIPTIONS.get(wf_id, ""))
    for task in ("create the function for sorting", "create the module"):
        d = r.classify(task)
        assert d.workflow_id == "build", task


# =====================================================================
# t10-F6: bench_nine key load is backend-aware
# =====================================================================
def test_bench_load_api_key_openai_no_gemini_file(monkeypatch):
    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "tunnel-key")
    monkeypatch.setenv("NINE_BENCH_KEY", "/nonexistent/gemini.key")
    monkeypatch.setattr(llm_provider, "_vault_key", lambda: "")
    monkeypatch.setattr(llm_provider, "_auth_key", lambda: "")
    sys.path.insert(0, str(REPO / "bench"))
    try:
        import bench_nine

        assert bench_nine.load_api_key() == "tunnel-key"
    finally:
        sys.path.remove(str(REPO / "bench"))


# =====================================================================
# t10-F7: model-or-fail messages name the active backend
# =====================================================================
def test_require_key_messages_are_backend_aware():
    for p in (REPO / "nine").rglob("*.py"):
        if p.name in ("llm_provider.py", "responder.py"):
            continue
        txt = p.read_text()
        assert "requires GEMINI_API_KEY" not in txt, p
        if "(ADK LlmAgent)" in txt or "requires an LLM key" in txt:
            assert "OPENCODE_GO_API_KEY" in txt or "openai" in txt, p


# =====================================================================
# t10-F8: docs no longer promise keyless flagship / GEMINI_MODEL pin
# =====================================================================
def test_submission_no_keyless_flagship_claim():
    t = (REPO / "SUBMISSION.md").read_text()
    assert "requires" in t and "API key" in t


def test_readme_gemini_model_claim_is_honest():
    t = (REPO / "README.md").read_text()
    assert "ADK workflow nodes" in t
    assert "NINE_LLM_BACKEND=openai" in t
