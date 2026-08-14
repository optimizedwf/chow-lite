"""Intent classifier — the ROUTE step of the nine loop.

Takes a task description and classifies it to a workflow using a Gemini
model, with the deterministic KeywordRouter as the routing substrate (it is
also the LEARN loop's write target: catalog.json keywords are learned, and
every task must still route to a model-gated workflow for execution).

Scope note: routing determinism is NOT output fabrication. The router may
decide without a model, but every workflow it can select (respond included)
requires a model at EXECUTE time — nine never fabricates answers.

Output conforms to schemas/route-decision.schema.json.
"""
from __future__ import annotations

import json
import math
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from nine.runtime import llm_provider
from nine.schema_validation import validate


@dataclass
class RouteDecision:
    """Schema-conformant route decision record."""
    decision_id: str
    task_redacted: str
    workflow_id: str
    confidence: float
    reason: str
    decided_at: str
    router_version: str
    alternatives: list[str] = field(default_factory=list)
    model: str = "deterministic-keyword"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


def redact(text: str) -> str:
    """Basic lexical redaction for credential-shaped strings.

    NOTE: this is not a security boundary — it reduces accidental secret
    leakage in logs (matching the design of the internal nine router).
    """
    patterns: list[tuple[str, str, re.RegexFlag]] = [
        # comparison chains (==, !=, ~=) FIRST so the plain [=:] pattern does
        # not steal the leading '=' of '==' and leak the secret tail.
        # torture-16 F4: consume the WHOLE chained comparison — a
        # `password == hunter2 == hunter3` must not leak the tail.
        (r"(password|passwd|pwd|secret|token|api[_-]?key)\s*[=!~]=\s*\S+(?:\s*[=!~]=\s*\S+)*", "\\1=***", re.DOTALL | re.IGNORECASE),
        # quoted values BEFORE the plain [=:] pattern so a space-containing
        # value is consumed whole — torture-16 F4: `"token": "sk-123 abc"`
        # and `api_key = "sk-123 abc def"` leaked everything after the first
        # space (\S+ stopped at it).
        (r"[\"'](password|passwd|pwd|secret|token|api[_-]?key|aws_secret_access_key|aws_access_key_id)[\"']\s*[:=]\s*[\"'][^\"']*[\"']", "\\1=***", re.DOTALL | re.IGNORECASE),
        (r"(password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*[\"'][^\"']*[\"']", "\\1=***", re.DOTALL | re.IGNORECASE),
        # plain key=value / key: value
        (r"(password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*\S+", "\\1=***", re.DOTALL | re.IGNORECASE),
        (r"(password|passwd|pwd|secret|token|api[_-]?key)\s+(?:is|was|:=|:|=)\s*\S+", "\\1=***", re.DOTALL | re.IGNORECASE),
        # torture-6 F4: JSON-quoted credentials ("api_key":"sk-123", "token": "abc")
        (r"[\"'](password|passwd|pwd|secret|token|api[_-]?key|aws_secret_access_key|aws_access_key_id)[\"']\s*[:=]\s*[\"']\S+[\"']", "\\1=***", re.DOTALL | re.IGNORECASE),
        # AWS keys: AKIA... and aws_secret_access_key = value
        (r"AKIA[0-9A-Z]{16}", "AKIA***", re.DOTALL | re.IGNORECASE),
        (r"aws_secret_access_key\s*[=:]\s*\S+", "aws_secret_access_key=***", re.DOTALL | re.IGNORECASE),
        (r"aws_access_key_id\s*[=:]\s*\S+", "aws_access_key_id=***", re.DOTALL | re.IGNORECASE),
        # Slack tokens: xoxb-/xoxp-/xapp-/xoxs-/xoxr-...
        (r"xox[baprs]-[0-9A-Za-z-]{10,}", "xox***", re.DOTALL | re.IGNORECASE),
        (r"Bearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer ***", re.DOTALL | re.IGNORECASE),
        # torture-15 F6: HTTP Basic auth header — Authorization: Basic <b64>
        (r"Authorization\s*:\s*Basic\s+[A-Za-z0-9+/=]{8,}", "Authorization: Basic ***", re.DOTALL | re.IGNORECASE),
        # torture-15 F6: URI userinfo (mongodb://user:pass@host, https://u:p@h)
        (r"([a-z][a-z0-9+.-]*://)[^/@\s]+@", "\\1***@", re.DOTALL | re.IGNORECASE),
        # torture-15 F6: CLI-style `--password hunter2` / `--token xyz`
        (r"(--(?:password|passwd|pwd|secret|token|api[_-]?key))\s+\S+", "\\1 ***", re.DOTALL | re.IGNORECASE),
        (r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", "***PRIVATE KEY***", re.DOTALL | re.IGNORECASE),
        # torture-16 F4: `\b` + explicit non-lowercase guard so innocent
        # words (skillfulness, skateboarding, pkill...) pass through; AIza
        # keys are always followed by letters (AIzaSy...) so they keep the
        # unguarded branch. Case-SENSITIVE (no IGNORECASE) so the guard is
        # meaningful — real keys (sk-ABC..., pk_live..., ghp_...) still match.
        (r"\b((?:sk|pk|gh[po])(?![a-z])|AIza)[A-Za-z0-9_\-]{10,}", "\\1***", re.DOTALL),
    ]
    out = text
    # torture T3-F8: case-insensitive — API_KEY=, PASSWORD=, TOKEN: all
    # leak verbatim today; uppercase credential forms are the common
    # ones in real tasks ("my API_KEY is ...").
    for pat, repl, fl in patterns:
        out = re.sub(pat, repl, out, flags=fl)
    return out


class KeywordRouter:
    """Deterministic routing substrate: keyword -> workflow.

    Used always as the routing backbone (CI/tests included) and as a
    baseline for the model router. Workflows are registered by id with
    a list of trigger keywords and a description.
    """

    def __init__(self, workflows: dict[str, dict] | None = None) -> None:
        # workflow_id -> {keywords: [...], description: str}
        self.workflows: dict[str, dict] = workflows or {}

    def register(self, workflow_id: str, keywords: list[str], description: str = "") -> None:
        self.workflows[workflow_id] = {
            "keywords": [k.lower() for k in keywords],
            "description": description,
        }

    def classify(self, task: str) -> tuple[str, float, str]:
        task_l = task.lower()
        best_id, best_score, best_kw = None, 0.0, ""
        for wf_id, meta in self.workflows.items():
            for kw in meta["keywords"]:
                # word-boundary match: "latest news" must NOT hit the `test`
                # lane (test ⊂ latest) and "water the plant" must NOT hit
                # `plan` (plan ⊂ plant) — substring routing misroutes tasks.
                if re.search(rf"\b{re.escape(kw)}\b", task_l):
                    # prefer longer (more specific) keywords
                    score = len(kw) / max(len(task_l), 1)
                    if score > best_score:
                        best_id, best_score, best_kw = wf_id, score, kw
        if best_id is None:
            # universal fallback: even an unknown task is a real workflow
            # (`respond`) so every prompt goes through EXECUTE + VERIFY.
            return "respond", 0.0, "no keyword matched; universal respond lane"
        return best_id, best_score, best_kw


_RETRY_DELAYS = (1.5, 3.0)  # seconds between retries (tests may shrink)


def _non_retryable(exc: Exception) -> bool:
    """True for client-side errors that retrying cannot fix (bad key, etc.)."""
    name = type(exc).__name__
    if name in ("InvalidArgument", "PermissionDenied", "Unauthenticated",
                "ApiKeyNotFoundError", "ValueError", "TypeError"):
        return True
    # google.genai errors carry status_code (e.g. 400 bad request)
    code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    return isinstance(code, int) and 400 <= code < 500 and code not in (408, 429)


class GeminiRouter:
    """Model router using Gemini 3.6 Flash via the Gemini API.

    The model is asked to pick ONE workflow from the registered catalog and
    return a JSON decision. Schema validation happens in Router.classify().
    """

    def __init__(self, model: Any, workflows: dict[str, dict] | None = None) -> None:
        self.model = model
        self.workflows = workflows or {}

    def _generate(self, prompt: str):
        """generate_content with timeout + retry/backoff on transient errors.

        Free-tier Gemini is quota'd (20 req/day, 5 req/min): 429/503 are
        NORMAL and retryable, so one burst must not silently drop the model
        route in favor of keywords.
        """
        import time

        for attempt in range(len(_RETRY_DELAYS) + 1):
            try:
                return self.model.generate_content(prompt)
            except Exception as exc:  # noqa: BLE001 - transient API errors retried
                if attempt >= len(_RETRY_DELAYS) or _non_retryable(exc):
                    raise
                time.sleep(_RETRY_DELAYS[attempt])
        raise RuntimeError("unreachable")

    def classify(self, task: str) -> tuple[str, float, str]:
        catalog = "\n".join(
            f"- {wf_id}: {meta['description']}" for wf_id, meta in self.workflows.items()
        )
        prompt = (
            "You are the routing layer of an agent operating system. "
            "Classify the task to exactly ONE workflow from this catalog:\n"
            f"{catalog}\n\n"
            f"Task: {task}\n\n"
            "Respond with JSON only: "
            '{"workflow_id": "...", "confidence": 0.0-1.0, "reason": "..."}'
        )
        resp = self._generate(prompt)
        try:
            txt = resp.text.strip()
            # strip markdown fences if present
            if txt.startswith("```"):
                txt = txt.split("\n", 1)[1].rsplit("```", 1)[0]
            data = json.loads(txt)
            wf_id = str(data.get("workflow_id", ""))
            conf = float(data.get("confidence", 0.0))
            # torture-5 F7: NaN/Infinity confidence would poison the ledger
            # (json.dumps emits bare NaN -> not strict JSON). Treat any
            # non-finite or out-of-range value as an unparsable response so
            # the caller falls back to the keyword substrate.
            if not math.isfinite(conf) or not (0.0 <= conf <= 1.0):
                raise ValueError(f"confidence out of range: {conf!r}")
            reason = str(data.get("reason", ""))
            return wf_id, conf, reason
        except Exception as exc:  # noqa: BLE001 — parse failure = NO decision:
            # return an empty workflow id so the Router falls back to the
            # keyword substrate instead of misrouting to `respond` and
            # stamping a model decision the model never produced.
            return "", 0.0, f"model output unparsable: {exc}"


def _provider_model_name() -> str:
    """Actual serving model of the active backend (t9-F5: no hardcoded ids)."""

    return llm_provider.model_name()

class Router:
    """Router facade: model first, keyword substrate fallback (routing only).

    Emits a schema-conformant RouteDecision with secret-redacted task text.
    """

    def __init__(
        self,
        workflows: dict[str, dict] | None = None,
        model: Any | None = None,
        version: str = "0.1.0",
    ) -> None:
        self.workflows = workflows or {}
        self.model = model
        self.version = version
        self.keyword = KeywordRouter(self.workflows)
        self.model_router: GeminiRouter | None = None
        if model is not None:
            self.model_router = GeminiRouter(model, self.workflows)

    def register(self, workflow_id: str, keywords: list[str], description: str = "") -> None:
        self.workflows[workflow_id] = {
            "keywords": [k.lower() for k in keywords],
            "description": description,
        }
        self.keyword.register(workflow_id, keywords, description)
        if self.model_router is not None:
            self.model_router.workflows = self.workflows

    def classify(self, task: str) -> RouteDecision:
        task_red = redact(task)
        wf_id, conf, reason = "", 0.0, ""
        model_used = "deterministic-keyword"
        fallback_note = ""

        if self.model_router is not None:
            try:
                wf_id, conf, reason = self.model_router.classify(task_red)
                # t9-F5: report the ACTUAL serving model, not a hardcoded id
                # (openai backend serves deepseek-v4-flash via the tunnel)
                model_used = (
                    getattr(self.model_router.model, "model_name", None)
                    or _provider_model_name()
                )
            except Exception as exc:  # noqa: BLE001 - quota/network errors must never crash the loop
                fallback_note = (
                    f"model unavailable ({type(exc).__name__}); keyword fallback"
                )
            if reason.startswith("model output unparsable"):
                # parse failure is NOT a model decision (and never a `respond`
                # route): fall through to the keyword substrate, honestly.
                wf_id, conf = "", 0.0
                fallback_note = fallback_note or "model output unparsable; keyword fallback"
            # validate against catalog; fall back if the model invented an id
            if wf_id not in self.workflows:
                wf_id, conf, reason = "", 0.0, "model returned unknown workflow, falling back"
                fallback_note = fallback_note or "model returned unknown workflow; keyword fallback"
            # torture-5 F7: NaN/Infinity confidence would poison the ledger
            # (json.dumps emits bare NaN). Guard at the Router level too —
            # defense in depth, in case a model adapter bypasses the parser.
            if not math.isfinite(conf) or not (0.0 <= conf <= 1.0):
                wf_id, conf, reason = "", 0.0, "model returned non-finite/out-of-range confidence, falling back"
                fallback_note = fallback_note or "model confidence invalid; keyword fallback"

        if not wf_id or wf_id not in self.workflows:
            wf_id, conf, reason = self.keyword.classify(task_red)
            model_used = "deterministic-keyword"
            if fallback_note:
                reason = f"{reason} [{fallback_note}]"

        decision = RouteDecision(
            decision_id=str(uuid.uuid4()),
            task_redacted=task_red[:500],
            workflow_id=wf_id,
            confidence=round(conf, 3),
            reason=reason[:300] or "keyword match",
            decided_at=datetime.now(UTC).isoformat(),
            router_version=self.version,
            model=model_used,
        )
        # P1-6: every boundary object is validated against its declared schema.
        validate("route-decision", decision.to_dict())
        return decision
