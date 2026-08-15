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


def _is_local_ollama() -> bool:
    """True when the [OI] base URL points at a local ollama server.

    ollama's /v1/chat/completions compatibility layer does NOT honor
    think:false the way /api/chat does — qwen3:8b still burns its whole
    max_tokens budget on reasoning (finish:"length", empty content, no
    tool call). The /api/chat endpoint with top-level think:false fully
    suppresses reasoning (verified 2026-08-15, ollama 0.32.13). Use
    /api/chat for 127.0.0.1/localhost ollama; keep /v1/chat/completions
    for the DS4/opencode tunnel and any other [OI] server.
    """
    base = base_url()
    return "127.0.0.1" in base or "localhost" in base


def _chat_endpoint() -> str:
    """The chat-completions endpoint for the active backend.

    - gemini backend: not used (genai SDK, not HTTP chat completions)
    - DS4/opencode tunnel: POST /v1/chat/completions ([OI] compatible)
    - local ollama: POST /api/chat (top-level think:false fully suppresses
      qwen3:8b reasoning; the /v1 shim only *reduces* it -> empty stream)
    """
    if _is_local_ollama():
        return f"{base_url()}/api/chat"
    return f"{base_url()}/chat/completions"


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
            f"{_chat_endpoint()}",
            headers={"Authorization": f"Bearer {api_key()}",
                     "Content-Type": "application/json"},
            json={"model": model or model_name(),
                  "messages": messages,
                  "temperature": 0.0,
                  "max_tokens": 2048,
                  # ollama /api/chat streams by default; force non-stream so
                  # resp.json() parses as one object (the [OI] /v1 shim is
                  # non-streaming by default, so this key is harmless there).
                  "stream": False,
                  # ollama 0.32.13 /v1: options.think is INVERTED (options.think=false
                  # actually ENABLES thinking via the /think suffix); only TOP-LEVEL
                  # think is honored. qwen3:8b with thinking ON burns the whole
                  # max_tokens budget on reasoning -> empty content -> empty stream.
                  "think": os.environ.get("NINE_THINK", "false").lower()
                           in ("1", "true", "yes")},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        # /api/chat (local ollama) returns {"message": {...}}; the [OI]
        # /v1 shim returns {"choices": [{"message": {...}}]}.
        if "message" in data:
            msg = data["message"]
        else:
            choices = data.get("choices") or []
            if not choices:
                return None
            msg = choices[0].get("message") or {}
        text = (msg.get("content") or "").strip()
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
            # from __future__ import annotations (debug_wf.py, flagship.py)
            # turns every annotation into a STRING ("str" not str) — the
            # identity checks below would all miss and every parameter
            # would degrade to {"type": "object"}, which qwen3:8b reads as
            # "accept anything" and fumbles tool calls nondeterministically
            # (slice-44: diagnose node sometimes wrote ROOT_CAUSE.md,
            # sometimes replied text-only -> "empty stream"). Normalize
            # string hints to their type first.
            if isinstance(hint, str):
                hint = {"str": str, "int": int, "float": float,
                        "bool": bool}.get(hint.strip(), object)
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
                                     # ollama /api/chat wants arguments as a
                                     # DICT; the [OI] /v1 shim wants a JSON
                                     # STRING. A string round-trip double-escapes
                                     # backslash-heavy content (markdown like
                                     # `\"true\"`) and ollama's /api/chat JSON
                                     # lexer 400s with "Value looks like
                                     # object, but can't find closing '}'"
                                     # (slice-44: nondeterministic empty
                                     # stream after the first tool round).
                                     "arguments": (fc.args or {}) if _is_local_ollama()
                                                  else _json.dumps(fc.args or {})},
                    })
                elif part.function_response:
                    fr = part.function_response
                    resp = fr.response or {}
                    # ollama /api/chat accepts a plain string tool result;
                    # the {"result": ...} JSON envelope is an [OI]/Gemini
                    # convention that double-escapes braces/backslashes in
                    # the tool's own text and can 400 the next turn. Send
                    # the raw string to local ollama, keep the envelope for
                    # the [OI] tunnel.
                    if _is_local_ollama():
                        tool_content = resp if isinstance(resp, str) else _json.dumps(resp)
                    else:
                        tool_content = _json.dumps(resp)
                    tool_msgs.append({
                        "role": "tool",
                        "tool_call_id": fr.id or "call_0",
                        "name": fr.name,
                        "content": tool_content,
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
            cfg_max = None
            if cfg is not None and getattr(cfg, "max_output_tokens", None):
                cfg_max = cfg.max_output_tokens
            # max_tokens is MANDATORY: a reasoning model (qwen3:8b) with no
            # cap generates reasoning tokens until the context truncates
            # (n_decoded 18K+ / truncated=1, slice-40 debugging) — an
            # infinite "thinking" loop that hangs the request. Local
            # default 4096 (code + reasoning headroom); NINE_MAX_TOKENS
            # overrides. Gemini/DS4 keep their ADK-specified value.
            try:
                _max_tok = int(os.environ.get("NINE_MAX_TOKENS", "4096"))
            except ValueError:
                _max_tok = 4096
            payload["max_tokens"] = cfg_max or _max_tok
            # qwen3:8b is a REASONING model: with thinking on it spends the
            # entire max_tokens budget on ... reasoning and NEVER emits the
            # tool call (slice-40: build hop 3x ~2min turns, no tool call,
            # empty stream). think:false forces direct tool-call emission.
            # Default ON for the local backend; NINE_THINK=false disables
            # (Gemini/DS4 ignore this field).
            # ollama 0.32.13 /v1: options.think is INVERTED (options.think=false
            # actually ENABLES thinking via the /think suffix); only TOP-LEVEL
            # think is honored. qwen3:8b with thinking ON burns the whole
            # max_tokens budget on reasoning -> empty content -> empty stream.
            payload["think"] = os.environ.get("NINE_THINK", "false").lower() in (
                "1", "true", "yes"
            )
            # ollama /api/chat streams by default; force non-stream so the
            # response is a single JSON object (harmless for [OI] /v1).
            payload["stream"] = False
            try:
                import requests

                # t9-F7: HTTP timeout — default 120s (Gemini/DS4 fast); a
                # slow local model (qwen3:8b thinking turns) can exceed 2
                # minutes per turn, so NINE_LLM_TIMEOUT_S raises it (600s
                # for local runs). Timeout -> HTTP 500 -> empty stream was
                # the local build-hop killer (slice 40 debugging).
                try:
                    _timeout_s = float(os.environ.get("NINE_LLM_TIMEOUT_S", "120"))
                except ValueError:
                    _timeout_s = 120.0
                resp = requests.post(
                    f"{_chat_endpoint()}",
                    headers={"Authorization": f"Bearer {api_key()}",
                             "Content-Type": "application/json"},
                    json=payload, timeout=_timeout_s,
                )
                if resp.status_code != 200:
                    yield _err(f"tunnel HTTP {resp.status_code}")
                    return
                data = resp.json()
                # /api/chat (local ollama) returns {"message": {...},
                # "done_reason": ...}; the [OI] /v1 shim returns
                # {"choices": [{"message": {...}}]}.
                if "message" in data:
                    msg = data["message"]
                else:
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
                    raw_args = fn.get("arguments")
                    if isinstance(raw_args, dict):
                        args = raw_args  # some [OI] servers return a dict directly
                    else:
                        try:
                            args = _json.loads(raw_args or "{}")
                        except Exception:  # noqa: BLE001 - malformed args -> {}
                            args = {}
                    _coerce_tool_args(args, fn.get("name"))
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

def _coerce_tool_args(args: dict[str, Any], tool_name: str | None = None) -> dict[str, Any]:
    """Normalize messy tool-call arguments from smaller/local [OI] models.

    qwen3-class models sometimes wrap string parameters in an object like
    {"content": "...", "type": "object"} (a nested JSON schema artifact) or
    emit numbers as strings. Every declared STRING parameter that arrives as
    a dict is unwrapped: prefer the keys content/text/value, then the first
    string value, else json.dumps. Callers (ADK FunctionTool) type-check
    against the tool signature, so this prevents "data must be str, not
    dict" crashes on real local-model traffic.
    """
    for key, val in list(args.items()):
        if isinstance(val, dict):
            for prefer in ("content", "text", "value", "message"):
                if isinstance(val.get(prefer), str):
                    args[key] = val[prefer]
                    break
            else:
                strs = [v for v in val.values() if isinstance(v, str)]
                args[key] = strs[0] if strs else _json.dumps(val)
        elif isinstance(val, (int, float)) and not isinstance(val, bool):
            args[key] = str(val)
    return args


