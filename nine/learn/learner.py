"""LEARN — the route-event learning loop (candidate-only self-improvement).

After every job the system records a *route event*: which workflow was
chosen, at what confidence, and what the evidence verdict was. The learner
turns those events into *improvement candidates* — proposed router tweaks
or gate additions — that are NEVER auto-applied. They go to a candidates
queue where a human (or a review hop) approves them.

This is the honest version of "self-improving agents": the system learns
from its own outcomes, but a human owns the changes. No silent feedback
loops, no drift.

Output schema: nine/schemas/route-event.schema.json
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from nine.schema_validation import validate


@dataclass
class RouteEvent:
    """One recorded route -> verdict observation."""
    event_id: str
    job_id: str
    task_redacted: str
    workflow_id: str
    confidence: float
    router_version: str
    verdict: str            # SHIP | FIX | BLOCK
    checks_passed: int
    checks_total: int
    recorded_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )
    fix_directive: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ImprovementCandidate:
    """A proposed change; requires human/review approval to apply."""
    candidate_id: str
    kind: str                # keyword | gate | workflow
    description: str
    evidence: list[str]      # route event ids that motivate this
    status: str = "pending"  # pending | approved | rejected | applied
    params: dict[str, Any] = field(default_factory=dict)  # machine-readable change spec
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RouteEventStore:
    """Append-only JSONL store of route events."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    def record(self, event: RouteEvent) -> None:
        validate("route-event", event.to_dict())
        with self.path.open("a") as f:
            f.write(json.dumps(event.to_dict()) + "\n")

    def all(self) -> list[RouteEvent]:
        out = []
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue  # one corrupt line must not brick event loading
            if not isinstance(rec, dict):
                continue
            try:
                out.append(RouteEvent(**rec))
            except TypeError:
                continue
        return out

    def by_workflow(self, workflow_id: str) -> list[RouteEvent]:
        return [e for e in self.all() if e.workflow_id == workflow_id]


class CandidateStore:
    """Durable JSONL store of improvement candidates (P1-5).

    Candidates survive restarts: they are appended on write and re-read
    from disk on every access, so the LEARN loop's OUTPUT is durable too,
    not just its input events.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.touch()

    def append(self, cand: ImprovementCandidate) -> None:
        with self.path.open("a") as f:
            f.write(json.dumps(cand.to_dict()) + "\n")

    def all(self) -> list[ImprovementCandidate]:
        out = []
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
            if not isinstance(rec, dict):
                continue
            try:
                out.append(ImprovementCandidate(**rec))
            except TypeError:
                continue
        return out

    def get(self, candidate_id: str) -> ImprovementCandidate | None:
        for c in self.all():
            if c.candidate_id == candidate_id:
                return c
        return None

    def update_status(self, candidate_id: str, status: str) -> None:
        """Rewrite the JSONL with a new status for one candidate (immutable
        log -> status is a state transition, applied in place)."""
        recs = []
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                recs.append(json.loads(line))
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
        changed = False
        for rec in recs:
            if rec.get("candidate_id") == candidate_id:
                rec["status"] = status
                changed = True
        if not changed:
            raise ValueError(f"no candidate {candidate_id}")
        self.path.write_text("".join(json.dumps(r) + "\n" for r in recs))

    def has(self, description: str, evidence: list[str]) -> bool:
        return any(
            c.description == description and c.evidence == evidence
            for c in self.all()
        )


class Learner:
    """Turns route events into improvement candidates (never auto-applies).

    Candidates are persisted next to the event store (P1-5): the queue
    survives restarts and learn() is idempotent per event — re-scanning
    the same events never duplicates candidates.
    """

    def __init__(self, store: RouteEventStore) -> None:
        self.store = store
        self.cands = CandidateStore(str(store.path) + ".candidates.jsonl")

    def observe(self, event: RouteEvent) -> None:
        self.store.record(event)

    def _suggest(
        self,
        kind: str,
        description: str,
        evidence: list[str],
        params: dict[str, Any] | None = None,
    ) -> None:
        if self.cands.has(description, evidence):
            return
        cand = ImprovementCandidate(
            candidate_id=f"cand-{uuid.uuid4().hex[:8]}",
            kind=kind,
            description=description,
            evidence=evidence,
            params=params or {},
        )
        self.cands.append(cand)

    def learn(self) -> list[ImprovementCandidate]:
        """Scan recorded events and propose improvements.

        Rules (all conservative, all candidate-only):
          * a workflow that repeatedly BLOCKs on the same missing artifact
            -> candidate: add a gate check requiring that artifact
          * a workflow that repeatedly routes to respond (or any lane) with
            low confidence -> candidate: add keywords / re-describe workflow
          * high-confidence routes that still FIX -> candidate: tighten gate
        """
        events = self.store.all()
        # an event seeds AT MOST one candidate ever (even after apply/reject):
        # otherwise the same observation re-suggests the next-best keyword on
        # every scan once the first suggestion was applied.
        used_events: set[str] = set()
        for c in self.cands.all():
            used_events.update(c.evidence)
        for ev in events:
            if ev.event_id in used_events:
                continue
            if ev.verdict == "BLOCK":
                self._suggest(
                    "gate",
                    f"workflow '{ev.workflow_id}' BLOCKed with fix_directive "
                    f"'{ev.fix_directive[:80]}'; consider a stricter gate or "
                    "a recovery hop",
                    [ev.event_id],
                )
            elif ev.verdict == "FIX" and ev.confidence >= 0.7:
                self._suggest(
                    "gate",
                    f"high-confidence route to '{ev.workflow_id}' still FIXed; "
                    "check workflow step reliability",
                    [ev.event_id],
                )
            elif ev.confidence < 0.3:
                # low-confidence route: propose adding a keyword so the next
                # identical task routes with more certainty (kind=keyword).
                # Only auto-applicable when the route DID reach a known
                # workflow (the strongest unmatched task token becomes the
                # keyword); an unregistered workflow id has no target
                # workflow, so its candidate requires a human decision.
                from nine.registry import WORKFLOWS

                kw = _derive_keyword(ev) if ev.workflow_id in WORKFLOWS else ""
                self._suggest(
                    "keyword",
                    f"route to '{ev.workflow_id}' at confidence "
                    f"{ev.confidence:.2f} (low); add keyword "
                    f"'{kw or '<human-chosen>'}' or re-describe the workflow",
                    [ev.event_id],
                    params={
                        "workflow_id": ev.workflow_id if ev.workflow_id in WORKFLOWS else "",
                        "keyword": kw or "",
                        "task_hint": ev.task_redacted[:80],
                    },
                )
        return self.cands.all()

    def candidates_json(self) -> str:
        return json.dumps([c.to_dict() for c in self.cands.all()], indent=2)


# words too generic to make good router keywords
_STOPWORDS = {
    "the", "and", "for", "with", "this", "that", "from", "what", "when",
    "task", "please", "need", "want", "make", "write", "some", "about",
    "would", "could", "should", "there", "their", "your", "have", "been",
    "into", "over", "under", "after", "before", "then", "than", "them",
}


def _derive_keyword(ev: RouteEvent) -> str:
    """Longest informative token in the task not already routing the workflow.

    The human owns the final choice (candidate-only doctrine); this just
    makes the candidate actionable for `nine learn apply`.
    """
    from nine.registry import KEYWORDS

    existing = set(KEYWORDS.get(ev.workflow_id, []))
    toks = [
        t for t in re.findall(r"[a-z]{4,}", ev.task_redacted.lower())
        if t not in _STOPWORDS and t not in existing
    ]
    if not toks:
        return ""
    return max(toks, key=len)[:30]
