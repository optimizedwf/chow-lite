"""Firestore-backed job ledger — the cloud deployment backend.

The JSONL ledger is the zero-dependency default (local/CI). For production
deployment on Google Cloud, the same Job API is backed by Firestore:

    collection: chowlite/jobs/{job_id}
    fields:     the full Job record (JSON-serializable)

The API mirrors JSONLLedger so swapping backends is a one-line change.
Requires GOOGLE_APPLICATION_CREDENTIALS (or ambient GCP metadata) and the
`google-cloud-firestore` package.

NOTE: this is the "durable state" layer that maps to ADK sessions/memory —
the job record + route events are the persistent memory of the system.
"""
from __future__ import annotations

from typing import Any

from chowlite.ledger.ledger import Job, LedgerError


class FirestoreLedger:
    """Firestore-backed ledger with the same API as JSONLLedger."""

    def __init__(self, project: str | None = None, collection: str = "chowlite-jobs") -> None:
        from google.cloud import firestore  # lazy import

        if project:
            self.db = firestore.Client(project=project)
        else:
            self.db = firestore.Client()
        self.collection = collection

    def _ref(self, job_id: str):
        return self.db.collection(self.collection).document(job_id)

    def submit(self, workflow_id: str, input: dict[str, Any] | None = None,
               chain_id: str | None = None) -> Job:
        job = Job(workflow_id=workflow_id, input=input, chain_id=chain_id)
        self._ref(job.job_id).set(job.to_dict())
        return job

    def get(self, job_id: str) -> Job:
        doc = self._ref(job_id).get()
        if not doc.exists:
            raise LedgerError(f"job not found: {job_id}")
        rec: dict[str, Any] = doc.to_dict() or {}
        job = Job(workflow_id=rec["workflow_id"], job_id=rec["job_id"])
        job.__dict__.update({k: v for k, v in rec.items() if k != "workflow_id"})
        return job

    def discover(self, status: str | None = None,
                 workflow_id: str | None = None) -> list[Job]:
        q: Any = self.db.collection(self.collection)
        if status:
            q = q.where("status", "==", status)
        if workflow_id:
            q = q.where("workflow_id", "==", workflow_id)
        jobs = []
        for doc in q.stream():
            rec: dict[str, Any] = doc.to_dict() or {}
            job = Job(workflow_id=rec["workflow_id"], job_id=rec["job_id"])
            job.__dict__.update({k: v for k, v in rec.items() if k != "workflow_id"})
            jobs.append(job)
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def transition(self, job_id: str, new_status: str) -> Job:
        job = self.get(job_id)
        job.transition(new_status)
        self._ref(job_id).update({"status": job.status, "updated_at": job.updated_at,
                                  "completed_at": job.completed_at})
        return job

    def update(self, job: Job) -> Job:
        self._ref(job.job_id).set(job.to_dict(), merge=True)
        return job

    def status(self, job_id: str) -> str:
        return self.get(job_id).status

    def artifacts(self, job_id: str) -> list[dict[str, Any]]:
        return self.get(job_id).artifacts

    def cancel(self, job_id: str) -> Job:
        return self.transition(job_id, "cancelled")

    def recover(self, job_id: str) -> Job:
        job = self.get(job_id)
        if job.status in ("blocked", "failed"):
            job.transition("recovered")
            self._ref(job_id).update({"status": job.status, "updated_at": job.updated_at})
        return job

    def stats(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        total = 0
        for doc in self.db.collection(self.collection).stream():
            total += 1
            s = (doc.to_dict() or {}).get("status", "?")
            counts[s] = counts.get(s, 0) + 1
        return {"total": total, "by_status": counts}
