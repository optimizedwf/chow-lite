"""FastAPI server — the Cloud Run entrypoint for chow-lite.

Exposes the operator API over HTTP so anyone can submit tasks, inspect
jobs, and see evidence verdicts. This is the surface the demo video shows
running on Google Cloud (the .run URL in the browser).

Endpoints:
    GET  /health          liveness
    POST /v1/submit       {"task": "..."}  -> route + run + verdict
    GET  /v1/jobs         list jobs (?status=shipped)
    GET  /v1/jobs/{id}    full job record
    GET  /v1/stats        ledger stats
"""
from __future__ import annotations

import os
import sys
from collections import defaultdict, deque
from pathlib import Path
from time import monotonic
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from chowlite.gates.evidence import (
    EvidenceGate,
    eval_json_check,
    exit_codes_check,
    required_artifact_check,
)
from chowlite.ledger.firestore_ledger import FirestoreLedger
from chowlite.ledger.ledger import LedgerError
from chowlite.router.classifier import Router
from chowlite.runtime.workflows import Node, Workflow, WorkflowExecutor, write_demo_artifacts

app = FastAPI(title="chow-lite", version="0.1.0")

# Cloud Run serves a read-only filesystem except /tmp; K_SERVICE is set by
# Cloud Run so data always lands on writable scratch. Override via CHOW_DATA_DIR.
_RUNTIME = Path(os.environ.get(
    "CHOW_DATA_DIR",
    "/tmp/chow-lite" if os.environ.get("K_SERVICE") else ".",
))
LEDGER_PATH = _RUNTIME / "jobs" / "ledger.jsonl"
WORKDIR = _RUNTIME / "work"
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")


_ledger: Any | None = None
_ledger_failed = False


def get_ledger():
    """Firestore in cloud, JSONL locally.

    Falls back cleanly BOTH at construction (missing creds) and on the first
    query failure (misconfigured emulator, transient gRPC error) so a broken
    Firestore can never take the API down.
    """
    global _ledger, _ledger_failed  # noqa: PLW0603
    if _ledger is not None and not _ledger_failed:
        return _ledger
    if _ledger_failed:
        from chowlite.ledger.ledger import JSONLLedger

        return JSONLLedger(LEDGER_PATH)
    try:
        from google.cloud import firestore  # noqa: F401

        candidate = FirestoreLedger(
            collection=os.environ.get("FIRESTORE_COLLECTION", "chowlite-jobs")
        )
        _ledger = _LazyFallbackLedger(candidate)
        return _ledger
    except Exception:  # noqa: BLE001 - deliberate fallback to JSONL when Firestore unavailable
        _ledger_failed = True
        from chowlite.ledger.ledger import JSONLLedger

        return JSONLLedger(LEDGER_PATH)


class _LazyFallbackLedger:
    """Proxy that swaps to JSONL if any Firestore query raises.

    Keeps the API alive even when Firestore is configured but unhealthy.
    """

    def __init__(self, primary: Any) -> None:
        self._primary = primary
        self._fallback = None

    def _resolve(self):
        if self._fallback is not None:
            return self._fallback
        try:
            return self._primary
        except Exception:  # noqa: BLE001
            return self._fallback

    def __getattr__(self, name: str):
        from chowlite.ledger.ledger import JSONLLedger

        primary_attr = getattr(self._primary, name)

        def wrapper(*args, **kwargs):
            try:
                return primary_attr(*args, **kwargs)
            except Exception:  # noqa: BLE001 - switch to JSONL on any query failure
                if self._fallback is None:
                    self._fallback = JSONLLedger(LEDGER_PATH)
                return getattr(self._fallback, name)(*args, **kwargs)

        return wrapper


class SubmitRequest(BaseModel):
    """Validated task payload — caps prompt size to bound Gemini bills."""
    task: str = Field(..., min_length=1, max_length=2000)


# --- lightweight auth + rate limiting (demo-appropriate; no OAuth needed) ---
_API_KEY = os.environ.get("CHOW_API_KEY", "")
MAX_BODY_BYTES = 1_048_576  # 1 MiB
RATE_LIMIT = {"window_s": 60.0, "max": 30}
_hits: dict[str, deque] = defaultdict(deque)


def _check_auth(x_api_key: str | None) -> JSONResponse | None:
    """Enforce X-API-Key only when CHOW_API_KEY is configured (demo default:
    unset = open. Set it before deploying publicly.)"""
    if _API_KEY and (x_api_key is None or x_api_key != _API_KEY):
        return JSONResponse({"detail": "missing/invalid X-API-Key"}, status_code=401)
    return None


def _check_rate_limit(request: Request) -> JSONResponse | None:
    ip = request.client.host if request.client else "unknown"
    now = monotonic()
    q = _hits[ip]
    while q and q[0] < now - RATE_LIMIT["window_s"]:
        q.popleft()
    if len(q) >= RATE_LIMIT["max"]:
        return JSONResponse({"detail": "rate limit exceeded"}, status_code=429)
    q.append(now)
    return None


@app.middleware("http")
async def _guard(request: Request, call_next):
    """Content-Length cap + (optional) auth + rate limit for mutating routes."""
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES:
        return JSONResponse({"detail": "payload too large"}, status_code=413)
    path = request.url.path
    if path.startswith("/v1/"):
        limited = _check_rate_limit(request)
        if limited is not None:
            return limited
        denied = _check_auth(request.headers.get("x-api-key"))
        if denied is not None:
            return denied
    return await call_next(request)


def build_router() -> Router:
    """Live Gemini 3.5 Flash routing when GEMINI_API_KEY is present;
    deterministic keyword fallback keeps the API usable offline/CI."""
    r = Router()
    r.register("research", ["research", "investigate", "find out", "study"],
               "Produce a findings document (research.md).")
    r.register("build", ["build", "implement", "write code", "create the"],
               "Implement from a plan; produce build artifacts + EVAL.json.")
    r.register("review", ["review", "audit", "check the code", "qa"],
               "Review a build; produce review.md verdict.")
    r.register("inbox-triage-task-report",
               ["trip", "plan", "refund", "customer", "inbox"],
               "Taskmaster lane: inbox -> triage -> task -> report.")
    r.register("respond", ["hello", "hi", "help", "what can you do"],
               "Direct answer; no execution run.")
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        try:
            from google import genai
            client = genai.Client(api_key=key)

            class _Model:
                def generate_content(self, prompt):
                    return client.models.generate_content(
                        model=os.environ.get("GEMINI_MODEL", "gemini-3.5-flash"),
                        contents=prompt)

            r = Router(model=_Model(), version="gemini-3.5-flash-live")
            r.register("research", ["research", "investigate", "find out", "study"],
                       "Produce a findings document (research.md).")
            r.register("build", ["build", "implement", "write code", "create the"],
                       "Implement from a plan; produce build artifacts + EVAL.json.")
            r.register("review", ["review", "audit", "check the code", "qa"],
                       "Review a build; produce review.md verdict.")
            r.register("inbox-triage-task-report",
                       ["trip", "plan", "refund", "customer", "inbox"],
                       "Taskmaster lane: inbox -> triage -> task -> report.")
            r.register("respond", ["hello", "hi", "help", "what can you do"],
                       "Direct answer; no execution run.")
        except Exception as exc:  # noqa: BLE001 - deliberate keyword fallback when model router fails
            # pragma: no cover - env-dependent
            print(f"live router unavailable ({exc}); using keyword fallback",
                  file=sys.stderr)
    return r


def build_gate() -> EvidenceGate:
    gate = EvidenceGate()
    gate.register_check("eval-json", eval_json_check())
    gate.register_check("artifacts", required_artifact_check(["FINAL_REPORT.md"]))
    gate.register_check("exit-codes", exit_codes_check())
    return gate


@app.get("/health")
def health():
    return {"status": "ok", "service": "chow-lite", "model": MODEL}


@app.post("/v1/submit")
def submit(payload: SubmitRequest):
    task = payload.task
    ledger = get_ledger()
    router = build_router()
    decision = router.classify(task)
    if decision.workflow_id in ("respond", "fallback-respond"):
        return {"decision": decision.to_dict(), "note": "direct answer; no run"}

    job = ledger.submit(workflow_id=decision.workflow_id, input={"task": task})
    job.attach_route_decision(decision)
    ledger.update(job)

    # demo workflow: deterministic node + evidence gate.
    # RCE-hardened: task text is written from Python, never interpolated
    # into a shell command.
    wf = Workflow(id=decision.workflow_id)
    wf.add_node(Node(
        id="collect", kind="tool",
        run=lambda inputs, jd: write_demo_artifacts(
            decision.workflow_id, task, Path(jd)),
        description="collect task + write report artifact + EVAL.json (Python)"))

    gate = build_gate()
    ex = WorkflowExecutor(ledger, gate, workdir=WORKDIR)
    result = ex.execute(wf, job, {"task": task})
    return {
        "job_id": job.job_id,
        "status": job.status,
        "verdict": result["verdict"],
        "decision": decision.to_dict(),
    }


@app.get("/v1/jobs")
def jobs(status: str | None = None):
    return {"jobs": [j.to_dict() for j in get_ledger().discover(status=status)]}


@app.get("/v1/jobs/{job_id}")
def job_detail(job_id: str):
    try:
        return get_ledger().get(job_id).to_dict()
    except LedgerError as e:
        raise HTTPException(404, str(e)) from e


@app.get("/v1/stats")
def stats():
    return get_ledger().stats()
