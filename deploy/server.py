"""FastAPI server — the Cloud Run entrypoint for nine.

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
from collections import defaultdict, deque
from pathlib import Path
from time import monotonic
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from nine.gates.evidence import (
    EvidenceGate,
    eval_json_check,
    exit_codes_check,
)
from nine.ledger.firestore_ledger import FirestoreLedger
from nine.ledger.ledger import LedgerError
from nine.router.classifier import Router
from nine.runtime.workflows import WorkflowError, WorkflowExecutor


class BodyLimitMiddleware:
    """ASGI-level body cap for mutating routes (torture-7 F6).

    The old guard only checked the Content-Length HEADER; a chunked/
    streamed body carries no content-length, so an unbounded body was fully
    buffered before validation (Cloud Run OOM / DoS). This middleware reads
    the body itself with a hard byte cap (413 the instant it is exceeded,
    without buffering the rest) and replays the bounded body downstream.
    """

    def __init__(self, app: Any, max_bytes: int | None = None) -> None:
        self.app = app
        # MAX_BODY_BYTES is defined later in this module; resolve at
        # instantiation (add_middleware runs after the constant exists).
        self.max_bytes = max_bytes if max_bytes is not None else MAX_BODY_BYTES

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # fast path: declared content-length over the cap
        for name, value in scope.get("headers", []):
            if name == b"content-length":
                try:
                    if int(value) > self.max_bytes:
                        await self._send_413(send)
                        return
                except ValueError:
                    pass
                break
        if scope.get("method", "GET") not in ("POST", "PUT", "PATCH"):
            await self.app(scope, receive, send)
            return
        # read the body with a hard cap (chunked bodies have no
        # content-length; raising from a receive wrapper gets swallowed by
        # Starlette into a 400, so read + cap here and replay below).
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            if message["type"] == "http.request":
                body.extend(message.get("body", b""))
                if len(body) > self.max_bytes:
                    await self._send_413(send)
                    return
                if not message.get("more_body", False):
                    break

        replayed = False

        async def replay_receive() -> dict:
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body),
                        "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _send_413(send: Any) -> None:
        await send({
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({
            "type": "http.response.body",
            "body": b'{"detail": "payload too large"}',
        })


app = FastAPI(title="nine", version="0.1.0")
app.add_middleware(BodyLimitMiddleware)


@app.exception_handler(WorkflowError)
async def _workflow_error_handler(request: Request, exc: WorkflowError) -> JSONResponse:
    """Fail loud: a model-gated job that cannot run returns a clean 502 with
    the reason — never a fabricated answer."""
    return JSONResponse({"detail": str(exc)}, status_code=502)

# Cloud Run serves a read-only filesystem except /tmp; K_SERVICE is set by
# Cloud Run so data always lands on writable scratch. Override via NINE_DATA_DIR.
_RUNTIME = Path(os.environ.get(
    "NINE_DATA_DIR",
    "/tmp/nine" if os.environ.get("K_SERVICE") else ".",
))
LEDGER_PATH = _RUNTIME / "jobs" / "ledger.jsonl"
WORKDIR = _RUNTIME / "work"
EVENTS_PATH = _RUNTIME / "jobs" / "events.jsonl"
MEMORY_PATH = _RUNTIME / "jobs" / "memory.jsonl"
MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")


_ledger: Any | None = None
_ledger_failed = False




def get_learner():
    """Durable LEARN store (JSONL in the runtime data dir; Firestore events
    are a future step — the route-event log is append-only and small)."""
    from nine.learn.learner import Learner, RouteEventStore

    return Learner(RouteEventStore(EVENTS_PATH))


def get_memory():
    """Semantic MemoryGraph (NINE_MEMORY=firestore on Cloud Run, JSONL local)."""
    from nine.memory.graph import get_memory_graph

    return get_memory_graph(path=MEMORY_PATH)
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
        from nine.ledger.ledger import JSONLLedger

        return JSONLLedger(LEDGER_PATH)
    try:
        from google.cloud import firestore  # noqa: F401

        candidate = FirestoreLedger(
            collection=os.environ.get("FIRESTORE_COLLECTION", "nine-jobs")
        )
        _ledger = _LazyFallbackLedger(candidate)
        return _ledger
    except Exception as exc:  # noqa: BLE001 - deliberate fallback to JSONL when Firestore unavailable
        _ledger_failed = True
        # P1-7: durability degradation must be LOUD, not silent — the Cloud
        # Run log shows exactly why we fell back to local JSONL state.
        print(
            f"[nine] WARNING: Firestore unavailable ({type(exc).__name__}: "
            f"{exc}); falling back to LOCAL JSONL ledger at {LEDGER_PATH}. "
            "Jobs will NOT survive a container restart.", flush=True
        )
        from nine.ledger.ledger import JSONLLedger

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
        from nine.ledger.ledger import JSONLLedger

        primary_attr = getattr(self._primary, name)

        def wrapper(*args, **kwargs):
            try:
                return primary_attr(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001 - switch to JSONL on any query failure
                if self._fallback is None:
                    print(
                        f"[nine] WARNING: Firestore query failed "
                        f"({type(exc).__name__}: {exc}); switching to LOCAL JSONL "
                        f"ledger at {LEDGER_PATH}. Jobs will NOT survive a "
                        "container restart.", flush=True
                    )
                    self._fallback = JSONLLedger(LEDGER_PATH)
                return getattr(self._fallback, name)(*args, **kwargs)

        return wrapper


class SubmitRequest(BaseModel):
    """Validated task payload — caps prompt size to bound Gemini bills."""
    task: str = Field(..., min_length=1, max_length=2000)


# --- lightweight auth + rate limiting (demo-appropriate; no OAuth needed) ---
_API_KEY = os.environ.get("NINE_API_KEY", "")
MAX_BODY_BYTES = 1_048_576  # 1 MiB
RATE_LIMIT = {"window_s": 60.0, "max": 30}
_hits: dict[str, deque] = defaultdict(deque)


def _check_auth(x_api_key: str | None) -> JSONResponse | None:
    """Enforce X-API-Key only when NINE_API_KEY is configured (demo default:
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
    """Live model routing when the active backend has a key; the KeywordRouter
    substrate (learned catalog keywords) otherwise. Default backend is Gemini
    3.6 Flash; NINE_LLM_BACKEND=openai routes via the testing tunnel (DS4
    Flash).

    Routing-only: a keyword route still lands in a real, model-gated
    workflow — nine never fabricates answers, it only decides the lane."""
    from nine.registry import HOP_DESCRIPTIONS, KEYWORDS
    from nine.runtime import llm_provider

    def _register(r: Router) -> None:
        for wf_id, kws in KEYWORDS.items():
            r.register(wf_id, kws, HOP_DESCRIPTIONS.get(wf_id, ""))

    r = Router()
    _register(r)
    model = llm_provider.make_model_client()
    if model is not None:
        r = Router(model=model, version=f"{llm_provider.model_name()}-live")
        _register(r)
    return r


def build_gate() -> EvidenceGate:
    """Generic single-shot gate. Per-hop gates live in the chain/hop
    definitions (ChainExecutor._gate_for); this one only verifies that
    EVAL.json exists with passing checks and no bash node crashed."""
    gate = EvidenceGate()
    gate.register_check("eval-json", eval_json_check())
    gate.register_check("exit-codes", exit_codes_check())
    return gate


@app.get("/health")
def health():
    return {"status": "ok", "service": "nine", "model": MODEL}


@app.post("/v1/submit")
def submit(payload: SubmitRequest):
    task = payload.task
    ledger = get_ledger()
    router = build_router()
    decision = router.classify(task)
    # EVERY prompt is a workflow: no direct-answer escape hatch. A task that
    # matches no specialist lane routes to `respond`, which still runs a job,
    # produces RESPONSE.md, and is verified before anything returns.
    job = ledger.submit(workflow_id=decision.workflow_id, input={"task": task})
    job.attach_route_decision(decision)
    ledger.update(job)

    # dispatch through the shared registry: the demo lane and the flagship
    # hops run their REAL multi-hop chains / workflows, not a canned echo.
    from nine.registry import CHAINS, WORKFLOWS

    gate = build_gate()
    if decision.workflow_id in CHAINS:
        from nine.chains.chain import ChainExecutor
        chain = CHAINS[decision.workflow_id]()
        job_dir = WORKDIR / job.job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "task.txt").write_text(task + "\n")
        if decision.workflow_id == "inbox-triage-task-report":
            (job_dir / "inbox.txt").write_text(task + "\n")
        cex = ChainExecutor(ledger, workdir=WORKDIR, learner=get_learner(),
                            memory=get_memory())
        res = cex.execute(chain, job, {"task": task}, decision=decision)
        return {
            "job_id": job.job_id,
            "status": job.status,
            "final": res["final"],
            "verdict": res.get("verdict", {}),
            "decision": decision.to_dict(),
        }

    if decision.workflow_id in WORKFLOWS:
        wf = WORKFLOWS[decision.workflow_id]()
    else:
        # unregistered workflow_id: fail loud. No collect node, no
        # fabricated EVAL.json — the router must only emit registered ids.
        from nine.runtime.workflows import WorkflowError

        raise WorkflowError(
            f"unregistered workflow id '{decision.workflow_id}' — no collect "
            "fallback; nine is model-driven (router must only emit "
            "registered ids)"
        )

    from nine.registry import workflow_gate

    gate = workflow_gate(decision.workflow_id) or build_gate()
    ex = WorkflowExecutor(ledger, gate, workdir=WORKDIR)
    result = ex.execute(wf, job, {"task": task})
    _record_route_event(get_learner(), job, decision, result["verdict"])
    body: dict[str, Any] = {
        "job_id": job.job_id,
        "status": job.status,
        "verdict": result["verdict"],
        "attempts": result.get("attempts", 1),
        "decision": decision.to_dict(),
    }
    if decision.workflow_id == "respond":
        resp_path = WORKDIR / job.job_id / "RESPONSE.md"
        if resp_path.exists():
            body["response"] = resp_path.read_text(encoding="utf-8").strip()
    return body


def _record_route_event(learner, job, decision, verdict: dict) -> None:
    """Append one route event (chain runs record per-hop events inside
    ChainExecutor; direct answers get verdict UNVERIFIED with no job)."""
    from nine.learn.learner import RouteEvent

    eval_results = verdict.get("eval_results") or {}
    learner.observe(
        RouteEvent(
            event_id=f"ev-{job.job_id[:8] if job else decision.task_redacted[:8]}",
            job_id=job.job_id if job else "",
            task_redacted=decision.task_redacted[:200],
            workflow_id=decision.workflow_id,
            confidence=float(decision.confidence),
            router_version=decision.router_version,
            verdict=verdict.get("verdict", "BLOCK"),
            checks_passed=sum(1 for r in eval_results.values() if r.get("passed")),
            checks_total=len(eval_results),
            fix_directive="",
        )
    )


@app.get("/v1/jobs")
def jobs(status: str | None = None):
    return {"jobs": [j.to_dict() for j in get_ledger().discover(status=status)]}


@app.get("/v1/jobs/{job_id}")
def job_detail(job_id: str):
    try:
        return get_ledger().get(job_id).to_dict()
    except LedgerError as e:
        raise HTTPException(404, str(e)) from e


@app.get("/v1/events")
def events(limit: int = 50):
    """Recent route events (the LEARN loop's raw material)."""
    learner = get_learner()
    all_ev = learner.store.all()
    return {
        "count": len(all_ev),
        "events": [e.to_dict() for e in all_ev[-limit:]],
    }


@app.get("/v1/stats")
def stats():
    data = get_ledger().stats()
    learner = get_learner()
    cands = learner.cands.all()
    data["events"] = {
        "count": len(learner.store.all()),
        "candidates": {
            "total": len(cands),
            "pending": sum(1 for c in cands if c.status == "pending"),
            "applied": sum(1 for c in cands if c.status == "applied"),
            "rejected": sum(1 for c in cands if c.status == "rejected"),
        },
    }
    return data
