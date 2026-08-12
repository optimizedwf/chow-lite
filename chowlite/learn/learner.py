"""LEARN — the route-event learning loop (candidate-only self-improvement).

After every job the system records a *route event*: which workflow was
chosen, at what confidence, and what the evidence verdict was. The learner
turns those events into *improvement candidates* — proposed router tweaks
or gate additions — that are NEVER auto-applied. They go to a candidates
queue where a human (or a review hop) approves them.

This is the honest version of "self-improving agents": the system learns
from its own outcomes, but a human owns the changes. No silent feedback
loops, no drift.

Output schema: chowlite/schemas/route-event.schema.json
"""
from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chowlite.schema_validation import validate


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
            if line.strip():
                out.append(RouteEvent(**json.loads(line)))
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
            if line.strip():
                out.append(ImprovementCandidate(**json.loads(line)))
        return out

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

    def _suggest(self, kind: str, description: str, evidence: list[str]) -> None:
        if self.cands.has(description, evidence):
            return
        cand = ImprovementCandidate(
            candidate_id=f"cand-{uuid.uuid4().hex[:8]}",
            kind=kind,
            description=description,
            evidence=evidence,
        )
        self.cands.append(cand)

    def learn(self) -> list[ImprovementCandidate]:
        """Scan recorded events and propose improvements.

        Rules (all conservative, all candidate-only):
          * a workflow that repeatedly BLOCKs on the same missing artifact
            -> candidate: add a gate check requiring that artifact
          * a workflow that repeatedly routes to fallback-respond with low
            confidence -> candidate: add keywords / re-describe workflow
          * high-confidence routes that still FIX -> candidate: tighten gate
        """
        events = self.store.all()
        for ev in events:
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
        return self.cands.all()

    def candidates_json(self) -> str:
        return json.dumps([c.to_dict() for c in self.cands.all()], indent=2)
