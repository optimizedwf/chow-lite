"""Hermetic tests for the nine LLM provider switch (torture round).

NINE_LLM_BACKEND=openai routes model nodes to the testing tunnel
(deepseek-v4-flash) while Gemini quota is exhausted; default backend stays
Gemini direct. Model-or-fail: providers return None on failure, callers
raise WorkflowError. All network calls are stubbed — nothing here touches
a real tunnel.
"""
from __future__ import annotations

import pytest

from nine.runtime import llm_provider


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


# ---------------------------------------------------------------------------
# backend / key selection
# ---------------------------------------------------------------------------
def test_backend_defaults_to_gemini(monkeypatch):
    monkeypatch.delenv("NINE_LLM_BACKEND", raising=False)
    assert llm_provider.backend() == "gemini"


@pytest.mark.parametrize("b", ["openai", "opencode", "rue", "OPENAI"])
def test_backend_openai_aliases(monkeypatch, b):
    monkeypatch.setenv("NINE_LLM_BACKEND", b)
    assert llm_provider.backend() == "openai"


def test_backend_junk_falls_back_to_gemini(monkeypatch):
    monkeypatch.setenv("NINE_LLM_BACKEND", "wat")
    assert llm_provider.backend() == "gemini"


def test_api_key_gemini_uses_gemini_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    assert llm_provider.api_key() == "g-key"


def test_api_key_openai_priority_chain(monkeypatch):
    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "env-key")
    assert llm_provider.api_key() == "env-key"
    monkeypatch.setenv("NINE_LLM_API_KEY", "explicit")
    assert llm_provider.api_key() == "explicit"


def test_api_key_openai_falls_back_to_vault_then_auth(monkeypatch):
    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setattr(llm_provider, "_vault_key", lambda: "vault-key")
    assert llm_provider.api_key() == "vault-key"
    monkeypatch.setattr(llm_provider, "_vault_key", lambda: "")
    monkeypatch.setattr(llm_provider, "_auth_key", lambda: "auth-key")
    assert llm_provider.api_key() == "auth-key"


def test_key_available_matches_api_key(monkeypatch):
    assert not llm_provider.key_available()
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert llm_provider.key_available()


def test_model_name_defaults(monkeypatch):
    assert llm_provider.model_name() == llm_provider.GEMINI_DEFAULT_MODEL
    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    assert llm_provider.model_name() == "deepseek-v4-flash"
    monkeypatch.setenv("NINE_LLM_MODEL", "custom/model")
    assert llm_provider.model_name() == "custom/model"


# ---------------------------------------------------------------------------
# chat_text (OpenAI-compatible REST)
# ---------------------------------------------------------------------------
def test_chat_text_returns_text(monkeypatch):
    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k")

    class FakeResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "hello world"}}]}

    def fake_post(url, headers, json, timeout):
        assert url == "https://opencode.ai/zen/go/v1/chat/completions"
        assert headers["Authorization"] == "Bearer k"
        assert json["model"] == "deepseek-v4-flash"
        assert json["messages"][-1] == {"role": "user", "content": "ping"}
        return FakeResp()

    monkeypatch.setattr("requests.post", fake_post)
    assert llm_provider.chat_text("ping") == "hello world"


def test_chat_text_model_or_fail_none_on_error(monkeypatch):
    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k")

    class FakeErr:
        status_code = 500

        def json(self):
            return {}

    monkeypatch.setattr("requests.post", lambda *a, **k: FakeErr())
    assert llm_provider.chat_text("ping") is None


def test_chat_text_none_on_empty_choices(monkeypatch):
    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k")

    class FakeResp:
        status_code = 200

        def json(self):
            return {"choices": []}

    monkeypatch.setattr("requests.post", lambda *a, **k: FakeResp())
    assert llm_provider.chat_text("ping") is None


def test_chat_text_refuses_without_key(monkeypatch):
    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    called = []

    def fake_post(*a, **k):
        called.append(1)
        raise AssertionError("must not POST without a key")

    monkeypatch.setattr("requests.post", fake_post)
    assert llm_provider.chat_text("ping") is None
    assert called == []


def test_chat_text_is_gemini_backend_noop(monkeypatch):
    assert llm_provider.chat_text("ping") is None


# ---------------------------------------------------------------------------
# make_model_client (Router adapter)
# ---------------------------------------------------------------------------
def test_make_model_client_openai_duck(monkeypatch):
    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k")

    class FakeResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "lane"}}]}

    monkeypatch.setattr("requests.post", lambda *a, **k: FakeResp())
    model = llm_provider.make_model_client()
    assert model is not None
    out = model.generate_content("which lane?")
    assert out.text == "lane"


def test_make_model_client_gemini_none_without_key(monkeypatch):
    assert llm_provider.make_model_client() is None


def test_make_model_client_gemini_with_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    model = llm_provider.make_model_client()
    assert model is not None
    assert hasattr(model, "generate_content")


# ---------------------------------------------------------------------------
# ADK override registration
# ---------------------------------------------------------------------------
def test_adk_override_noop_on_gemini_backend(monkeypatch):
    llm_provider.install_adk_override()
    assert llm_provider._ADK_OVERRIDE_INSTALLED is False


def test_adk_override_takes_over_gemini_registry(monkeypatch):
    from google.adk.models import registry

    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k")
    before = registry._llm_registry_dict.get("gemini-.*")
    llm_provider.install_adk_override()
    assert llm_provider._ADK_OVERRIDE_INSTALLED is True
    assert registry._llm_registry_dict["gemini-.*"] is not before
    resolved = registry.LLMRegistry.resolve("gemini-3.6-flash")
    assert resolved is registry._llm_registry_dict["gemini-.*"]
    # idempotent
    llm_provider.install_adk_override()
    assert registry._llm_registry_dict["gemini-.*"] is resolved


def test_uninstall_restores_lazy_gemini(monkeypatch):
    from google.adk.models import registry

    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k")
    llm_provider.install_adk_override()
    llm_provider.uninstall_adk_override()
    assert llm_provider._ADK_OVERRIDE_INSTALLED is False
    assert registry._llm_registry_dict["gemini-.*"] == (
        "google.adk.models.google_llm", "Gemini")


def test_adk_override_generates_via_tunnel(monkeypatch):
    import asyncio

    from google.adk.models import registry
    from google.adk.models.llm_request import LlmRequest
    from google.genai import types

    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k")
    llm_provider.install_adk_override()

    class FakeResp:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "adk hello"}}]}

    monkeypatch.setattr("requests.post", lambda *a, **k: FakeResp())
    inst = registry._llm_registry_dict["gemini-.*"](model="gemini-3.6-flash")
    lr = LlmRequest(
        model="gemini-3.6-flash",
        contents=[types.Content(role="user", parts=[types.Part(text="hi")])],
    )

    async def _collect():
        return [r async for r in inst.generate_content_async(lr)]

    responses = asyncio.run(_collect())
    assert len(responses) == 1
    resp = responses[0]
    assert resp.finish_reason == "STOP"
    assert resp.content.parts[0].text == "adk hello"


def test_adk_override_error_yields_error_response(monkeypatch):
    import asyncio

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

    responses = asyncio.run(_collect())
    assert responses[0].finish_reason == "ERROR"
    assert "429" in responses[0].error_message


# ---------------------------------------------------------------------------
# runtime dispatch (gemma / responder / summarizer via tunnel)
# ---------------------------------------------------------------------------
def test_gemma_generate_dispatches_to_tunnel(monkeypatch):
    from nine.runtime import gemma

    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setattr(
        llm_provider, "chat_text",
        lambda prompt, timeout=90: "teach text")
    assert gemma.gemma_generate("learn X") == "teach text"


def test_responder_dispatches_to_tunnel(monkeypatch):
    from nine.runtime import responder

    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k")
    monkeypatch.setattr(
        llm_provider, "chat_text",
        lambda prompt, timeout=120: "answer text")
    text, model = responder.respond_text("a task", max_chars=400)
    assert text == "answer text"
    assert model == "deepseek-v4-flash"


def test_responder_raises_without_key(monkeypatch):
    from nine.runtime import responder
    from nine.runtime.workflows import WorkflowError

    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    with pytest.raises(WorkflowError):
        responder.respond_text("a task", max_chars=400)


def test_summarizer_dispatches_to_tunnel(monkeypatch):
    from nine.runtime import summarizer

    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setattr(
        llm_provider, "chat_text",
        lambda prompt, timeout=90: "distilled")
    assert summarizer._gemini_generate("long doc", None) == "distilled"


def test_summarizer_raises_when_tunnel_silent(monkeypatch):
    from nine.runtime import summarizer
    from nine.runtime.workflows import WorkflowError

    monkeypatch.setenv("NINE_LLM_BACKEND", "openai")
    monkeypatch.setattr(llm_provider, "chat_text", lambda prompt, timeout=90: None)
    with pytest.raises(WorkflowError):
        summarizer._gemini_generate("long doc", None)
