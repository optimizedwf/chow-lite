"""Gemma 4 support — an *additional* Google AI model in the fleet.

nine's primary model is Gemini 3.6 Flash (mandatory). Gemma 4 gives the
teach hop a second Google model, unlocking the Stage-3 judging bonus
("+0.2 per additional Google AI model"). The call is a plain REST request
so it needs no extra dependencies.

Model-or-fail contract: gemma_generate returns None when no key / HTTP
error / no candidates / exception. It never fabricates output — CALLERS
must fail loud on None (see flagship._teach_gemma_node). There is no
offline/deterministic lesson fallback.
"""
from __future__ import annotations

import os

try:
    import requests as _requests
except ImportError:  # pragma: no cover
    _requests = None  # type: ignore[assignment]

requests = _requests

DEFAULT_MODEL = "gemma-4-26b-a4b-it"
API_BASE = "https://generativelanguage.googleapis.com/v1beta/models"


def gemma_generate(
    prompt: str,
    model: str | None = None,
    api_key: str | None = None,
    timeout: int = 90,
) -> str | None:
    """Call the teach model. Gemini backend: Gemma 4 via the Gemini REST API;
    testing backend (NINE_LLM_BACKEND=openai): DS4 Flash via the tunnel.
    Returns text or None on any failure."""
    from nine.runtime import llm_provider

    if llm_provider.backend() == "openai":
        # testing mode: the teach hop speaks to the tunnel (model-or-fail:
        # None -> caller raises WorkflowError)
        return llm_provider.chat_text(prompt, timeout=timeout)
    key = api_key or os.environ.get("GEMINI_API_KEY", "").strip()
    if not key or requests is None:
        return None
    model = model or DEFAULT_MODEL
    try:
        resp = requests.post(
            f"{API_BASE}/{model}:generateContent",
            headers={"Content-Type": "application/json",
                     "x-goog-api-key": key},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        cands = data.get("candidates") or []
        if not cands:
            return None
        parts = cands[0].get("content", {}).get("parts") or []
        return "".join(p.get("text", "") for p in parts).strip() or None
    except Exception:  # noqa: BLE001 — fallback is the contract
        return None
