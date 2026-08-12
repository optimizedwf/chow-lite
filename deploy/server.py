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
from pathlib import Path

from fastapi import FastAPI, HTTPException

from chowlite.gates.evidence import (
    EvidenceGate, eval_json_check, required_artifact_check, exit_codes_check,
)
from chowlite.ledger.firestore_ledger import FirestoreLedger
from chowlite.ledger.ledger import LedgerError
from chowlite.router.classifier import Router
from chowlite.runtime.workflows import Node, Workflow, WorkflowExecutor

app = FastAPI(title="chow-lite", version="0.1.0")

LEDGER_PATH = Path("jobs/ledger.jsonl")
WORKDIR = Path("work")
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")


def get_ledger():
    """Firestore in cloud, JSONL locally (falls back cleanly)."""
    try:
        from google.cloud import firestore  # noqa: F401
        return FirestoreLedger(
            collection=os.environ.get("FIRESTORE_COLLECTION", "chowlite-jobs")
        )
    except Exception:
        from chowlite.ledger.ledger import JSONLLedger
        return JSONLLedger(LEDGER_PATH)


def build_router() -> Router:
    r = Router()
    r.register("research", ["research", "investigate", "find out", "study"],
               "Produce a findings document (research.md).")
    r.register("build", ["build", "implement", "write code", "create the"],
               "Implement from a plan; produce build artifacts + EVAL.json.")
    r.register("review", ["review", "audit", "check the code", "qa"],
               "Review a build; produce review.md verdict.")
    r.register("respond", ["hello", "hi", "help", "what can you do"],
               "Direct answer; no execution run.")
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
def submit(payload: dict):
    task = payload.get("task", "")
    if not task:
        raise HTTPException(400, "task is required")
    ledger = get_ledger()
    router = build_router()
    decision = router.classify(task)
    if decision.workflow_id in ("respond", "fallback-respond"):
        return {"decision": decision.to_dict(), "note": "direct answer; no run"}

    job = ledger.submit(workflow_id=decision.workflow_id, input={"task": task})
    job.attach_route_decision(decision)
    ledger.update(job)

    # demo workflow: deterministic node + evidence gate
    wf = Workflow(id=decision.workflow_id)
    eval_json = ('{"checks":[{"name":"report-exists","passed":true,'
                 '"message":"FINAL_REPORT.md written"}]}')
    cmd = (f"echo '{task[:200]}' > task.txt; "
           f"printf 'Artifact: {decision.workflow_id}\n' > FINAL_REPORT.md; "
           f"printf '{eval_json}' > EVAL.json")
    wf.add_node(Node(id="collect", kind="bash", command=cmd))

    gate = build_gate()
    ex = WorkflowExecutor(ledger, gate, workdir=WORKDIR)
    result = ex.execute(wf, job, {"task": task})
    return {"job_id": job.job_id, "status": job.status, "verdict": result["verdict"]}


@app.get("/v1/jobs")
def jobs(status: str | None = None):
    return {"jobs": [j.to_dict() for j in get_ledger().discover(status=status)]}


@app.get("/v1/jobs/{job_id}")
def job_detail(job_id: str):
    try:
        return get_ledger().get(job_id).to_dict()
    except LedgerError as e:
        raise HTTPException(404, str(e))


@app.get("/v1/stats")
def stats():
    return get_ledger().stats()
