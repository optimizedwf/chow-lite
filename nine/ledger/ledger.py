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

from nine.router.classifier import RouteDecision
from nine.schema_validation import validate

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


def _looks_like_job(rec: dict) -> bool:
    """Shape guard for ledger records (torture-6 F3): a loaded line must look
    like a Job before we trust its fields — status must be a known value and
    list-valued fields must actually be lists."""
    if not isinstance(rec.get("status", "submitted"), str):
        return False
    if rec.get("status", "submitted") not in VALID_STATUSES:
        return False
    for field in ("artifacts", "verdicts"):
        v = rec.get(field, [])
        if not isinstance(v, list):
            return False
    if not isinstance(rec.get("metadata", {}), dict):
        return False
    for field in ("attempts", "max_fix_loops"):
        v = rec.get(field, 0)
        if not isinstance(v, int) or isinstance(v, bool):
            return False
    inp = rec.get("input", {})
    if not isinstance(inp, dict):
        return False
    for field in ("created_at", "updated_at"):
        v = rec.get(field)
        if v is not None and not isinstance(v, str):
            return False
    return True


class JSONLLedger:
    """Zero-dependency JSONL-backed ledger. One JSON object per line.

    Append-only for auditability: transitions write a new line; the last
    line for a job_id is the current state. (The internal nine design used
    JSONL for the same reason — audit + replay.)
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._jobs: dict[str, Job] = {}
        # (line_number, reason) for records that could not be parsed. A
        # crash mid-append or a hand-edit must NOT brick the whole ledger:
        # skip the bad line, keep the healthy jobs, report the damage.
        self.corrupt_lines: list[tuple[int, str]] = []
        try:
            self._load()
        except OSError as e:
            raise LedgerError(f"cannot read ledger {self.path}: {e}") from e

    def _load(self) -> None:
        if not self.path.exists():
            return
        # torture-6 F2: a single non-UTF8 byte used to raise UnicodeDecodeError
        # here and brick EVERY nine command (the per-line json try/except never
        # got a chance to run). Read with errors="replace" so the corrupt line
        # is skipped and counted like any other bad line.
        try:
            text = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            raise LedgerError(f"cannot read ledger {self.path}: {e}") from e
        for idx, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except (json.JSONDecodeError, TypeError, ValueError):
                self.corrupt_lines.append((idx, "not valid JSON"))
                continue
            if not isinstance(rec, dict):
                self.corrupt_lines.append((idx, "not an object"))
                continue
            wf_id = rec.get("workflow_id")
            jid = rec.get("job_id")
            if not wf_id or not jid:
                self.corrupt_lines.append((idx, "missing workflow_id/job_id"))
                continue
            # torture-6 F3: valid JSON with garbage fields used to crash later
            # calls (cancel -> KeyError on unknown status; artifacts ->
            # TypeError on a non-list). Validate the schema here; a record
            # that does not look like a Job is a corrupt line, not a bomb.
            if not _looks_like_job(rec):
                self.corrupt_lines.append((idx, "schema mismatch"))
                continue
            job = Job(workflow_id=wf_id, job_id=jid)
            job.__dict__.update(
                {k: v for k, v in rec.items() if k not in ("workflow_id", "job_id")}
            )
            self._jobs[jid] = job

    def _append(self, job: Job) -> None:
        try:
            with open(self.path, "a") as f:
                f.write(json.dumps(job.to_dict()) + "\n")
        except OSError as e:
            raise LedgerError(f"cannot append to ledger {self.path}: {e}") from e

    def submit(self, workflow_id: str, input: dict[str, Any] | None = None,
               chain_id: str | None = None) -> Job:
        # Redact at the LEDGER boundary (torture T4-F4): every submit path —
        # CLI submit, CLI chain, POST /v1/submit — stores the same redacted
        # task. Idempotent: callers may pre-redact; the boundary applies it
        # once more harmlessly. Execution still uses the RAW task (task.txt).
        if input and isinstance(input.get("task"), str):
            from nine.router.classifier import redact

            input = dict(input)
            input["task"] = redact(input["task"])
        job = Job(workflow_id=workflow_id, input=input, chain_id=chain_id)
        validate("agent-job", job.to_dict())
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
        """Recovery: BLOCKED/FAILED -> recovered -> running (re-execution).

        Any other status raises LedgerError — recovering a shipped job would
        destroy its verified artifacts and then crash on an illegal
        transition (torture T3-F3/T4-F2). Attempts are reset so the
        re-execution gets a full fix-loop budget.
        """
        job = self.get(job_id)
        if job.status not in ("blocked", "failed"):
            raise LedgerError(
                f"job {job_id} is {job.status}, only blocked/failed can be recovered"
            )
        job.transition("recovered")
        job.attempts = 0
        self._append(job)
        return job

    def stats(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        for j in self._jobs.values():
            counts[j.status] = counts.get(j.status, 0) + 1
        out: dict[str, Any] = {"total": len(self._jobs), "by_status": counts}
        if self.corrupt_lines:
            out["corrupt_lines"] = len(self.corrupt_lines)
        return out

    def snapshot(self, path: str | Path) -> None:
        """Export full snapshot (JSON list) — for backup/migration."""
        out = [j.to_dict() for j in self._jobs.values()]
        Path(path).write_text(json.dumps(out, indent=2))
