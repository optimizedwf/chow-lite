"""LLM provider switch for nine (torture doctrine: testing on DS4 Flash).

Default backend is Google Gemini DIRECT (status quo — every model node
requires GEMINI_API_KEY and fails loud without it). While the Gemini quota
is exhausted (quota 0, cooldown), BENCH can still run in TESTING MODE by
pointing the same model nodes at an OpenAI-compatible tunnel:

    NINE_LLM_BACKEND=openai   # activates the tunnel backend
    NINE_LLM_BASE_URL=...     # default https://opencode.ai/zen/go/v1
    NINE_LLM_API_KEY=...      # default: $OPENCODE_GO_API_KEY ->
                              #   ~/.agent-vault/keys/opencode-go.key ->
                              #   ~/.prime/agent/auth.json [opencode-go]
    NINE_LLM_MODEL=...        # default deepseek-v4-flash

Model-or-fail contract is preserved on BOTH backends: chat_text / model
clients return None on ANY failure and CALLERS raise WorkflowError. There
is no offline/deterministic fallback anywhere. Backend selection is a pure
function of the environment: default (unset / gemini) changes NOTHING.
"""
from __future__ import annotations

import json as _json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

GEMINI_DEFAULT_MODEL = "gemini-3.6-flash"
OPENAI_DEFAULT_BASE = "https://opencode.ai/zen/go/v1"
OPENAI_DEFAULT_MODEL = "deepseek-v4-flash"


_BACKEND_WARNED: set[str] = set()


def backend() -> str:
    """'gemini' (default) or 'openai' (testing tunnel).

    Unknown NON-EMPTY NINE_LLM_BACKEND values warn loudly (once per value,
    to stderr) instead of silently switching to the gemini backend — a typo
    like ``openai `` (trailing space) or ``OPENAI_TUNNEL`` must never burn
    real Gemini quota while the operator believes the tunnel is active.
    """
    raw = os.environ.get("NINE_LLM_BACKEND", "")
    b = raw.strip().lower()
    if b in ("openai", "opencode", "rue"):
        return "openai"
    if raw.strip() and b not in _BACKEND_WARNED:
        _BACKEND_WARNED.add(b)
        print(
            f"[nine.llm_provider] WARNING: unknown NINE_LLM_BACKEND={raw!r}; "
            "valid values: openai|opencode|rue (tunnel/testing) or unset "
            "(gemini default). Falling back to the GEMINI backend.",
            file=sys.stderr,
        )
    return "gemini"


def _vault_key() -> str:
    try:
        return (Path.home() / ".agent-vault" / "keys" / "opencode-go.key").read_text().strip()
    except Exception:  # noqa: BLE001 - missing/unreadable key file -> empty key
        return ""


def _auth_key() -> str:
    try:
        auth = _json.loads((Path.home() / ".prime" / "agent" / "auth.json").read_text())
        return str((auth.get("opencode-go") or {}).get("key", "")).strip()
    except Exception:  # noqa: BLE001 - missing/unreadable auth.json -> empty key
        return ""


def api_key() -> str:
    """The active backend's API key (never logged). Empty string = none."""
    if backend() == "openai":
        return (
            os.environ.get("NINE_LLM_API_KEY", "").strip()
            or os.environ.get("OPENCODE_GO_API_KEY", "").strip()
            or _vault_key()
            or _auth_key()
        )
    return os.environ.get("GEMINI_API_KEY", "").strip()


def key_available() -> bool:
    """Model-or-fail guard: True when the ACTIVE backend has a key."""
    return bool(api_key())


def model_name() -> str:
    """The model id the active backend serves (defaults per backend)."""
    if backend() == "openai":
        return os.environ.get("NINE_LLM_MODEL", OPENAI_DEFAULT_MODEL).strip()
    return os.environ.get("GEMINI_MODEL", GEMINI_DEFAULT_MODEL).strip()


def base_url() -> str:
    return os.environ.get("NINE_LLM_BASE_URL", OPENAI_DEFAULT_BASE).strip().rstrip("/")


def chat_text(
    prompt: str,
    *,
    system: str | None = None,
    model: str | None = None,
    timeout: int = 90,
) -> str | None:
    """OpenAI-compatible chat completion (tunnel backend only).

    Returns the model text or None on ANY failure — callers raise
    WorkflowError (model-or-fail). Never called on the gemini backend.
    """
    if backend() != "openai":
        return None
    if not api_key():
        return None
    try:
        import requests

        messages: list[dict[str, Any]] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})
        resp = requests.post(
            f"{base_url()}/chat/completions",
            headers={"Authorization": f"Bearer {api_key()}",
                     "Content-Type": "application/json"},
            json={"model": model or model_name(),
                  "messages": messages,
                  "temperature": 0.0,
                  "max_tokens": 2048},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return None
        text = ((choices[0].get("message") or {}).get("content") or "").strip()
        return text or None
    except Exception:  # noqa: BLE001 - model-or-fail: None, caller fails loud
        return None


def make_model_client() -> Any | None:
    """Duck-typed model client for the Router: .generate_content(prompt)
    returns an object with .text (None on failure — routing degrades to the
    keyword substrate, execution still requires the model)."""
    if backend() == "openai":
        if not api_key():
            return None

        class _OpenAIModel:
            def generate_content(self, prompt: str) -> SimpleNamespace:
                return SimpleNamespace(text=chat_text(prompt))

        return _OpenAIModel()
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        return None
    try:
        from google import genai

        client = genai.Client(api_key=key)

        class _GeminiModel:
            def generate_content(self, prompt: str):
                return client.models.generate_content(
                    model=model_name(), contents=prompt)

        return _GeminiModel()
    except ImportError:
        return None


def adk_model() -> Any:
    """LlmAgent `model=` argument for ADK workflow nodes.

    gemini backend: the real Gemini instance (status quo — all workflow
    nodes construct `Gemini(model="gemini-3.6-flash")`).
    openai backend: the registry STRING `gemini-3.6-flash` so
    install_adk_override()'s `gemini-.*` takeover resolves the LlmAgent to
    the tunnel (instance-based models bypass LLMRegistry — t9-F4).
    """
    if backend() == "openai":
        return GEMINI_DEFAULT_MODEL
    from google.adk.models import Gemini

    return Gemini(model=GEMINI_DEFAULT_MODEL)


# ---------------------------------------------------------------------------
# ADK override (testing mode): LlmAgent(model="gemini-3.6-flash") -> tunnel.
# ---------------------------------------------------------------------------
_ADK_OVERRIDE_INSTALLED = False


def install_adk_override() -> None:
    """Register an OpenAI-compatible ADK LLM so existing workflow nodes
    resolve to the tunnel when NINE_LLM_BACKEND=openai.

    Deliberately REPLACES the built-in 'gemini-.*' registry entry (testing
    mode redirects every Gemini-bound ADK node to DS4 Flash). No-op on the
    gemini backend; idempotent.
    """
    global _ADK_OVERRIDE_INSTALLED  # noqa: PLW0603 - idempotent install flag
    if _ADK_OVERRIDE_INSTALLED or backend() != "openai":
        return
    _ADK_OVERRIDE_INSTALLED = True
    try:
        from google.adk.models import registry
        from google.adk.models.base_llm import BaseLlm
        from google.adk.models.llm_request import LlmRequest
        from google.adk.models.llm_response import LlmResponse
        from google.genai import types
    except ImportError:
        return

    def _schema_for(func: Any) -> dict[str, Any]:
        """Minimal JSON-schema converter for tool args (str/int/float/bool/
        optional/list/dict). Unknown annotations degrade to open objects."""
        import inspect as _i

        props: dict[str, Any] = {}
        required: list[str] = []
        try:
            sig = _i.signature(func)
        except (TypeError, ValueError):
            return {"type": "object", "properties": {}}
        for name, p in sig.parameters.items():
            if name in ("self", "cls") or p.kind in (
                _i.Parameter.VAR_POSITIONAL, _i.Parameter.VAR_KEYWORD,
            ):
                continue
            hint = p.annotation
            if hint is _i.Parameter.empty:
                props[name] = {"type": "string"}
            elif hint is str:
                props[name] = {"type": "string"}
            elif hint is int:
                props[name] = {"type": "integer"}
            elif hint is float:
                props[name] = {"type": "number"}
            elif hint is bool:
                props[name] = {"type": "boolean"}
            else:
                props[name] = {"type": "object"}
            if p.default is _i.Parameter.empty:
                required.append(name)
        return {"type": "object", "properties": props, "required": required}

    def _openai_tools(llm_request: LlmRequest) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        raw = llm_request.tools_dict or {}
        if isinstance(raw, dict):
            items = raw.values()
        else:
            items = raw
        for tool in items:
            tool_func = getattr(tool, "func", None)
            name = getattr(tool, "name", None) or (tool_func.__name__ if tool_func else "tool")
            desc = getattr(tool, "description", "") or ""
            func = getattr(tool, "func", None)
            tools.append({
                "type": "function",
                "function": {
                    "name": name,
                    "description": desc,
                    "parameters": _schema_for(func) if func else {
                        "type": "object", "properties": {}},
                },
            })
        return tools

    def _messages_from(llm_request: LlmRequest) -> list[dict[str, Any]]:
        """genai Contents -> OpenAI messages (incl. tool rounds).

        t9-F2: LlmAgent.instruction arrives as config.system_instruction and
        must become a leading system message (agents otherwise run with NO
        system prompt in testing mode).
        t9-F3/t10-F3: function_response parts must produce EXACTLY ONE tool
        message per response — no duplicate tool messages, and no spurious
        empty user message between an assistant tool_calls turn and the tool
        result (google-adk sends tool results as role="user" parts).
        """
        out: list[dict[str, Any]] = []
        cfg = getattr(llm_request, "config", None)
        sysinst = getattr(cfg, "system_instruction", None) if cfg is not None else None
        if sysinst:
            if isinstance(sysinst, str):
                out.append({"role": "system", "content": sysinst})
            else:  # genai Content with text parts
                text = "".join(
                    getattr(pt, "text", "") or ""
                    for pt in (getattr(sysinst, "parts", None) or [])
                )
                if text.strip():
                    out.append({"role": "system", "content": text})
        for content in llm_request.contents:
            role = getattr(content, "role", "user") or "user"
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            tool_msgs: list[dict[str, Any]] = []
            for part in (content.parts or []):
                if part.text:
                    text_parts.append(part.text)
                elif part.function_call:
                    fc = part.function_call
                    tool_calls.append({
                        "id": fc.id or f"call_{len(tool_calls)}",
                        "type": "function",
                        "function": {"name": fc.name,
                                     "arguments": _json.dumps(fc.args or {})},
                    })
                elif part.function_response:
                    fr = part.function_response
                    tool_msgs.append({
                        "role": "tool",
                        "tool_call_id": fr.id or "call_0",
                        "name": fr.name,
                        "content": _json.dumps(fr.response or {}),
                    })
            if tool_msgs and not tool_calls and not text_parts:
                # pure tool-result content (role user OR tool): tool msgs only
                out.extend(tool_msgs)
                continue
            if role == "model":
                m: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
                if tool_calls:
                    m["tool_calls"] = tool_calls
                out.append(m)
                continue
            if role == "tool":
                out.extend(tool_msgs)
                continue
            # user content
            text = "".join(text_parts)
            if text:
                out.append({"role": "user", "content": text})
            if tool_msgs:
                out.extend(tool_msgs)
        return out

    class _OpenAILlm(BaseLlm):
        """ADK LLM that speaks OpenAI chat-completions to the tunnel."""

        @classmethod
        def supported_models(cls) -> list[str]:
            return [r"^gemini-[0-9].*$", r"^deepseek-v4-flash$"]

        async def generate_content_async(self, llm_request, stream=False):
            import json as _json

            def _err(msg: str) -> LlmResponse:
                # t9-F7: "ERROR" is not a valid genai FinishReason (emits a
                # UserWarning per failure + breaks the empty-content STOP
                # guard) — map errors to the valid OTHER member.
                return LlmResponse(
                    content=types.Content(role="model", parts=[]),
                    partial=False, finish_reason=types.FinishReason.OTHER,
                    error_message=msg,
                )

            # t9-F7: never POST without a key (mirror chat_text's guard).
            if not api_key():
                yield _err("tunnel key missing (model-or-fail: no POST without a key)")
                return
            payload: dict[str, Any] = {
                "model": model_name(),
                "messages": _messages_from(llm_request),
                "temperature": 0.0,
            }
            tools = _openai_tools(llm_request)
            if tools:
                payload["tools"] = tools
            cfg = llm_request.config
            if cfg is not None and getattr(cfg, "max_output_tokens", None):
                payload["max_tokens"] = cfg.max_output_tokens
            try:
                import requests

                resp = requests.post(
                    f"{base_url()}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key()}",
                             "Content-Type": "application/json"},
                    json=payload, timeout=120,
                )
                if resp.status_code != 200:
                    yield _err(f"tunnel HTTP {resp.status_code}")
                    return
                data = resp.json()
                choices = data.get("choices") or []
                if not choices:
                    yield _err("tunnel empty choices")
                    return
                msg = choices[0].get("message") or {}
                parts: list[Any] = []
                if msg.get("content"):
                    parts.append(types.Part(text=msg["content"]))
                for tc in msg.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    try:
                        args = _json.loads(fn.get("arguments") or "{}")
                    except Exception:  # noqa: BLE001 - malformed args -> {}
                        args = {}
                    parts.append(types.Part(function_call=types.FunctionCall(
                        id=tc.get("id"), name=fn.get("name", ""), args=args)))
                yield LlmResponse(
                    content=types.Content(role="model", parts=parts),
                    partial=False, finish_reason=types.FinishReason.STOP,
                )
            except Exception as exc:  # noqa: BLE001 - model-or-fail
                yield _err(f"tunnel call failed: {exc}")

    # register + take over the built-in gemini-.* entry (testing mode)
    registry.LLMRegistry.register(_OpenAILlm)
    registry._llm_registry_dict["gemini-.*"] = _OpenAILlm
    registry._llm_registry_dict["^gemini-.*$"] = _OpenAILlm


def uninstall_adk_override() -> None:
    """Test helper: FULLY drop the override and restore Gemini resolution.

    t9-F1 (critical): install adds BOTH registry keys (via
    LLMRegistry.register: `^gemini-[0-9].*$`, `^deepseek-v4-flash$`) and the
    manual takeover keys (`gemini-.*`, `^gemini-.*$`); resolve() is
    lru-cached. A partial uninstall left _OpenAILlm resolvable AFTER
    "restore" — with the backend flipped back to gemini, api_key() returns
    GEMINI_API_KEY and the stale override POSTs the real Gemini key to the
    tunnel host. Pop EVERY added key, clear the resolve cache, and restore
    the lazy Gemini entry so post-uninstall resolution is the original.
    """
    global _ADK_OVERRIDE_INSTALLED  # noqa: PLW0603 - test helper resets the flag
    _ADK_OVERRIDE_INSTALLED = False
    try:
        from google.adk.models import registry

        for key in (
            "^gemini-[0-9].*$",   # added by LLMRegistry.register()
            "^deepseek-v4-flash$",  # added by LLMRegistry.register()
            "gemini-.*",           # manual takeover
            "^gemini-.*$",         # manual takeover
        ):
            registry._llm_registry_dict.pop(key, None)
        try:
            registry.LLMRegistry.resolve.cache_clear()
        except Exception:  # noqa: BLE001 - cache may not exist in some ADK builds
            pass
        registry.LLMRegistry._register_lazy(
            ["gemini-.*"], "google.adk.models.google_llm", "Gemini")
    except Exception:  # noqa: BLE001 - best-effort test helper
        pass

