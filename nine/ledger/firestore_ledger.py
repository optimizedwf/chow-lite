"""Firestore-backed job ledger — the cloud deployment backend.

The JSONL ledger is the zero-dependency default (local/CI). For production
deployment on Google Cloud, the same Job API is backed by Firestore:

    collection: nine/jobs/{job_id}
    fields:     the full Job record (JSON-serializable)

The API mirrors JSONLLedger so swapping backends is a one-line change.
Requires GOOGLE_APPLICATION_CREDENTIALS (or ambient GCP metadata) and the
`google-cloud-firestore` package.

NOTE: this is the "durable state" layer that maps to ADK sessions/memory —
the job record + route events are the persistent memory of the system.
"""
from __future__ import annotations

import re
from typing import Any

from nine.ledger.ledger import VALID_STATUSES, Job, LedgerError


class FirestoreLedger:
    """Firestore-backed ledger with the same API as JSONLLedger."""

    def __init__(self, project: str | None = None, collection: str = "nine-jobs") -> None:
        from google.cloud import firestore  # lazy import

        if project:
            self.db = firestore.Client(project=project)
        else:
            self.db = firestore.Client()
        self.collection = collection
        # T19-F6 (slice 37): the cli.py recover path touches ledger._jobs
        # (torture-10 F1 cache sync) and ledger.refresh(); JSONLLedger has
        # both, FirestoreLedger had neither -> AttributeError on the
        # documented `nine recover --force` path. Keep the same in-memory
        # mirror (populated by get/refresh) so the backend swap is real.
        self._jobs: dict[str, Job] = {}

    # torture-28 F8: Firestore document ids must not contain "/" (path
    # separator) or "." segments (silently normalized/escaping) — an id
    # like "a/b" resolves to a nested subpath and ".." addresses outside
    # the intended namespace. Reject them as a clean LedgerError (the JSONL
    # backend already 404s these; the parity claim must hold on Firestore).
    _JOB_ID_OK = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

    def _ref(self, job_id: str):
        if not self._JOB_ID_OK.match(job_id):
            raise LedgerError(f"job not found: {job_id}")
        return self.db.collection(self.collection).document(job_id)

    def submit(self, workflow_id: str, input: dict[str, Any] | None = None,
               chain_id: str | None = None) -> Job:
        # torture-18 F1: the redact()/validate() boundary existed ONLY on
        # JSONLLedger — Cloud Run (the production backend, preferred by
        # deploy/server.py get_ledger) wrote RAW task text (AKIA/sk-/password
        # values the user pasted) verbatim into Firestore, and accepted
        # workflow_ids agent-job validation rejects. Mirror JSONLLedger.submit
        # exactly: redact at the boundary, validate before persisting.
        if input and isinstance(input.get("task"), str):
            from nine.router.classifier import redact

            input = dict(input)
            input["task"] = redact(input["task"])
        job = Job(workflow_id=workflow_id, input=input, chain_id=chain_id)
        from nine.schema_validation import validate

        validate("agent-job", job.to_dict())
        self._ref(job.job_id).set(job.to_dict())
        return job

    @staticmethod
    def _job_from_rec(rec: dict[str, Any]) -> Job | None:
        """Shape guard (torture-21 F6): a doc missing the required identity
        fields degrades like the JSONL ledger's tolerant loader (clean
        LedgerError / skip), never a raw KeyError/AttributeError -> HTTP 500.
        Firestore docs are console-editable and version-driftable, so a
        partial doc must not take down /v1/jobs/{id} or discover()."""
        workflow_id = rec.get("workflow_id")
        job_id = rec.get("job_id")
        if not isinstance(workflow_id, str) or not workflow_id or \
                not isinstance(job_id, str) or not job_id:
            return None
        # torture-24 F2: T21-F6's shape guard only checked the identity
        # fields — a console-edited / version-drifted doc with a wrong-typed
        # created_at or status still raw-TypeError'd discover()'s sorted()
        # ('<' not supported between NoneType/str, e.g. created_at: null)
        # and stats()'s dict-key bucket (unhashable 'dict') -> HTTP 500 for
        # every caller. Type-check the fields the API actually consumes and
        # skip the doc (JSONL parity: JSONLLedger._looks_like_job validates
        # the same shapes — str status from the whitelist, list fields,
        # per-entry shape).
        # created_at is what discover() sorts on: None/12345 would TypeError
        # the sorted() -> skip the doc. (updated_at/completed_at may be None
        # legitimately — a fresh Job's completed_at is None — so they only
        # need to be str-or-None.)
        if "created_at" in rec and not isinstance(rec["created_at"], str):
            return None
        for field in ("updated_at", "completed_at"):
            if field in rec and not isinstance(rec[field], (str, type(None))):
                return None
        if "status" in rec and not isinstance(rec["status"], str):
            return None
        if rec.get("status") not in (None, *VALID_STATUSES):
            return None
        for field in ("artifacts", "verdicts"):
            if field in rec and not isinstance(rec[field], list):
                return None
        for art in rec.get("artifacts", []):
            if not isinstance(art, dict) or "name" not in art:
                return None
        for vd in rec.get("verdicts", []):
            if not isinstance(vd, dict):
                return None
        if "attempts" in rec and not isinstance(rec["attempts"], int):
            return None
        job = Job(workflow_id=workflow_id, job_id=job_id)
        job.__dict__.update(
            {k: v for k, v in rec.items() if k != "workflow_id"})
        return job

    def get(self, job_id: str) -> Job:
        doc = self._ref(job_id).get()
        if not doc.exists:
            raise LedgerError(f"job not found: {job_id}")
        rec: dict[str, Any] = doc.to_dict() or {}
        job = self._job_from_rec(rec)
        if job is None:
            raise LedgerError(f"job not found: {job_id}")  # JSONL parity: clean 404
        self._jobs[job_id] = job  # mirror for the cli.py cache-sync contract
        return job

    def refresh(self, job_id: str) -> Job:
        """Firestore reads are always durable — same contract as
        JSONLLedger.refresh (re-read durable state; cache NOT rebuilt)."""
        return self.get(job_id)

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
            job = self._job_from_rec(rec)
            if job is None:
                continue  # skip malformed docs (JSONL parity: tolerant load)
            jobs.append(job)
        return sorted(jobs, key=lambda j: j.created_at, reverse=True)

    def transition(self, job_id: str, new_status: str) -> Job:
        job = self.get(job_id)
        job.transition(new_status)
        self._ref(job_id).update({"status": job.status, "updated_at": job.updated_at,
                                  "completed_at": job.completed_at})
        return job

    def update(self, job: Job) -> Job:
        # torture-18 F1: validate the FULL record on update too (the raw
        # merge=True set bypassed the agent-job boundary on every mutation).
        from nine.schema_validation import validate

        validate("agent-job", job.to_dict())
        self._ref(job.job_id).set(job.to_dict(), merge=True)
        return job

    def status(self, job_id: str) -> str:
        return self.get(job_id).status

    def artifacts(self, job_id: str) -> list[dict[str, Any]]:
        return self.get(job_id).artifacts

    def cancel(self, job_id: str) -> Job:
        return self.transition(job_id, "cancelled")

    def recover(self, job_id: str) -> Job:
        # T19-F6 (slice 37): this was a SILENT no-op for submitted/shipped
        # jobs — it returned the job unchanged, so the cli.py recover path
        # proceeded to WIPE the verified job dir and re-execute. Mirror the
        # JSONLLedger contract: any status other than blocked/failed raises
        # LedgerError; attempts are reset so the re-run gets a full budget.
        job = self.get(job_id)
        if job.status not in ("blocked", "failed"):
            raise LedgerError(
                f"job {job_id} is {job.status}, only blocked/failed can be recovered"
            )
        job.transition("recovered")
        job.attempts = 0
        # torture-29 F2: mirror the JSONLLedger run_seq bump (torture-27
        # F1). Without it a Firestore-backed recovery records its route
        # event under the ORIGINAL run's event id (cli.py builds
        # ev-<jobid>-<run_seq>) and Learner.learn() dedupes the re-run
        # away — LEARN is blind to verdict flips on Firestore deployments
        # exactly as the JSONL path was before T27-F1.
        job.metadata["run_seq"] = int(job.metadata.get("run_seq", 0)) + 1
        self._jobs[job_id] = job
        self._ref(job_id).update({
            "status": job.status,
            "updated_at": job.updated_at,
            "attempts": job.attempts,
            "metadata": job.metadata,
        })
        return job

    def stats(self) -> dict[str, Any]:
        counts: dict[str, int] = {}
        total = 0
        for doc in self.db.collection(self.collection).stream():
            total += 1
            s = (doc.to_dict() or {}).get("status", "?")
            # torture-24 F2 (belt): an unhashable status (dict/list) would
            # TypeError in counts[s] — bucket it under "?" like any other
            # malformed value instead of 500ing /v1/stats.
            if not isinstance(s, str):
                s = "?"
            counts[s] = counts.get(s, 0) + 1
        return {"total": total, "by_status": counts}
