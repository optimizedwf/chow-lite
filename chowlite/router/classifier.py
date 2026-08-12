"""Intent classifier — the ROUTE step of the chow-lite loop.

Takes a task description and classifies it to a workflow using a Gemini model
(with a deterministic keyword fallback so the core loop works even without
network/model access).

Output conforms to schemas/route-decision.schema.json.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any


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
    leakage in logs (matching the design of the internal chow router).
    """
    patterns = [
        (r"(password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*\S+", "\\1=***"),
        (r"Bearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer ***"),
        (r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", "***PRIVATE KEY***"),
        (r"(sk|pk|ghp|gho|AIza)[A-Za-z0-9_\-]{10,}", "\\1***"),
    ]
    out = text
    for pat, repl in patterns:
        out = re.sub(pat, repl, out, flags=re.DOTALL)
    return out


class KeywordRouter:
    """Deterministic fallback router: keyword -> workflow.

    Used when no model is available (offline / CI / tests) and as a
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
                if kw in task_l:
                    # prefer longer (more specific) keywords
                    score = len(kw) / max(len(task_l), 1)
                    if score > best_score:
                        best_id, best_score, best_kw = wf_id, score, kw
        if best_id is None:
            return "fallback-respond", 0.0, ""
        return best_id, best_score, best_kw


class GeminiRouter:
    """Model router using Gemini 3.5 Flash via the Gemini API.

    The model is asked to pick ONE workflow from the registered catalog and
    return a JSON decision. Schema validation happens in Router.classify().
    """

    def __init__(self, model: Any, workflows: dict[str, dict] | None = None) -> None:
        self.model = model
        self.workflows = workflows or {}

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
        resp = self.model.generate_content(prompt)
        try:
            txt = resp.text.strip()
            # strip markdown fences if present
            if txt.startswith("```"):
                txt = txt.split("\n", 1)[1].rsplit("```", 1)[0]
            data = json.loads(txt)
            wf_id = str(data.get("workflow_id", ""))
            conf = float(data.get("confidence", 0.0))
            reason = str(data.get("reason", ""))
            return wf_id, conf, reason
        except Exception as exc:  # noqa: BLE001 — fall back on any parse issue
            return "fallback-respond", 0.0, f"model output unparsable: {exc}"


class Router:
    """Router facade: model first, deterministic keyword fallback.

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
                model_used = "gemini-3.5-flash"
            except Exception as exc:  # noqa: BLE001 - quota/network errors must never crash the loop
                fallback_note = (
                    f"model unavailable ({type(exc).__name__}); keyword fallback"
                )
            # validate against catalog; fall back if the model invented an id
            if wf_id not in self.workflows:
                wf_id, conf, reason = "", 0.0, "model returned unknown workflow, falling back"
                fallback_note = fallback_note or "model returned unknown workflow; keyword fallback"

        if not wf_id or wf_id not in self.workflows:
            wf_id, conf, reason = self.keyword.classify(task_red)
            model_used = "deterministic-keyword"
            if fallback_note:
                reason = f"{reason} [{fallback_note}]"

        return RouteDecision(
            decision_id=str(uuid.uuid4()),
            task_redacted=task_red[:500],
            workflow_id=wf_id,
            confidence=round(conf, 3),
            reason=reason[:300] or "keyword match",
            decided_at=datetime.now(UTC).isoformat(),
            router_version=self.version,
            model=model_used,
        )
