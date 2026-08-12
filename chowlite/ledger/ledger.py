"""Durable job ledger — the durable record of every workflow execution.

Jobs are first-class citizens with a typed lifecycle:
    submitted -> routing -> running -> awaiting_evidence
              -> shipped | fixing -> running | blocked | cancelled
              -> recovered -> running
              -> archived

An exit code is NOT success. A job only reaches `shipped` when the evidence
gate returns SHIP. Everything else is UNVERIFIED until proven.

Storage backends: JSONL file (default, zero-dependency) and Firestore
(cloud deployment, via the runtime store adapter).
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from chowlite.router.classifier import RouteDecision

VALID_STATUSES = {
    "submitted", "routing", "running", "awaiting_evidence",
    "shipped", "fixing", "blocked", "cancelled", "recovered", "failed", "archived",
}

LEGAL_TRANSITIONS = {
    "submitted": {"routing", "cancelled"},
    "routing": {"running", "blocked", "cancelled"},
    "running": {"awaiting_evidence", "fixing", "failed", "cancelled"},
    "awaiting_evidence": {"shipped", "fixing", "blocked", "cancelled"},
    "fixing": {"running", "blocked", "cancelled"},
    "blocked": {"recovered", "cancelled", "archived"},
    "recovered": {"running", "cancelled"},
    "shipped": {"archived"},
    "failed": {"recovered", "cancelled", "archived"},
    "cancelled": {"archived"},
    "archived": set(),
}


class LedgerError(Exception):
    pass


class InvalidTransition(LedgerError):
    pass


class Job:
    """One job record: an execution of a workflow with its state + verdicts."""

    def __init__(
        self,
        workflow_id: str,
        input: dict[str, Any] | None = None,
        job_id: str | None = None,
        chain_id: str | None = None,
        max_fix_loops: int = 2,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        self.job_id = job_id or str(uuid.uuid4())
        self.workflow_id = workflow_id
        self.chain_id = chain_id
        self.status = "submitted"
        self.input = input or {}
        self.artifacts: list[dict[str, Any]] = []
        self.verdicts: list[dict[str, Any]] = []
        self.attempts = 0
        self.max_fix_loops = max_fix_loops
        self.route_decision: dict[str, Any] | None = None
        self.created_at = now
        self.updated_at = now
        self.completed_at: str | None = None
        self.metadata: dict[str, Any] = {}

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "workflow_id": self.workflow_id,
            "chain_id": self.chain_id,
            "status": self.status,
            "input": self.input,
            "route_decision": self.route_decision,
            "artifacts": self.artifacts,
            "verdicts": self.verdicts,
            "attempts": self.attempts,
            "max_fix_loops": self.max_fix_loops,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "metadata": self.metadata,
        }

    def transition(self, new_status: str) -> None:
        if new_status not in VALID_STATUSES:
            raise InvalidTransition(f"unknown status: {new_status}")
        if new_status not in LEGAL_TRANSITIONS[self.status]:
            raise InvalidTransition(
                f"illegal transition {self.status} -> {new_status}"
            )
        self.status = new_status
        self.updated_at = datetime.now(UTC).isoformat()
        if new_status in ("shipped", "blocked", "failed", "cancelled", "archived"):
            self.completed_at = self.updated_at

    def attach_route_decision(self, decision: RouteDecision) -> None:
        self.route_decision = decision.to_dict()
        self.updated_at = datetime.now(UTC).isoformat()

    def add_artifact(self, artifact: dict[str, Any]) -> None:
        self.artifacts.append(artifact)
        self.updated_at = datetime.now(UTC).isoformat()

    def add_verdict(self, verdict: dict[str, Any]) -> None:
        self.verdicts.append(verdict)
        self.updated_at = datetime.now(UTC).isoformat()


class JSONLLedger:
    """Zero-dependency JSONL-backed ledger. One JSON object per line.

    Append-only for auditability: transitions write a new line; the last
    line for a job_id is the current state. (The internal chow design used
    JSONL for the same reason — audit + replay.)
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            rec = json.loads(line)
            job = Job(workflow_id=rec["workflow_id"], job_id=rec["job_id"])
            job.__dict__.update({k: v for k, v in rec.items() if k != "workflow_id"})
            self._jobs[rec["job_id"]] = job

    def _append(self, job: Job) -> None:
        with open(self.path, "a") as f:
            f.write(json.dumps(job.to_dict()) + "\n")

    def submit(self, workflow_id: str, input: dict[str, Any] | None = None,
               chain_id: str | None = None) -> Job:
        job = Job(workflow_id=workflow_id, input=input, chain_id=chain_id)
        self._jobs[job.job_id] = job
        self._append(job)
        return job

    def get(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise LedgerError(f"job not found: {job_id}")
        return job

    def discover(self, status: str | None = None, workflow_id: str | None = None) -> list[Job]:
        jobs = list(self._jobs.values())
        if status:
            jobs = [j for j in jobs if j.status == status]
        if workflow_id:
            jobs = [j for j in jobs if j.workflow_id == workflow_id]
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def transition(self, job_id: str, new_status: str) -> Job:
        job = self.get(job_id)
        job.transition(new_status)
        self._append(job)
        return job

    def update(self, job: Job) -> Job:
        self._append(job)
        return job

    def status(self, job_id: str) -> str:
        return self.get(job_id).status

    def artifacts(self, job_id: str) -> list[dict[str, Any]]:
        return self.get(job_id).artifacts

    def cancel(self, job_id: str) -> Job:
        return self.transition(job_id, "cancelled")

    def recover(self, job_id: str) -> Job:
        """Recovery: BLOCKED/FAILED -> recovered -> running (re-execution)."""
        job = self.get(job_id)
        if job.status in ("blocked", "failed"):
            job.transition("recovered")
            self._append(job)
        return job

    def stats(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for j in self._jobs.values():
            counts[j.status] = counts.get(j.status, 0) + 1
        return {"total": len(self._jobs), "by_status": counts}

    def snapshot(self, path: str | Path) -> None:
        """Export full snapshot (JSON list) — for backup/migration."""
        out = [j.to_dict() for j in self._jobs.values()]
        Path(path).write_text(json.dumps(out, indent=2))
