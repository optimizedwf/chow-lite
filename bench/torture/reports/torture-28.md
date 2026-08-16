# TORTURE-TESTER-28 Report — deploy/server.py FastAPI surface (adversarial round)

- round: 28 (server-focused adversarial pass)
- scope: `deploy/server.py` FastAPI surface + its direct dependencies (ledger/learn/memory/registry/classifier/gates) — hermetic pytest / `python -c` only, zero Gemini quota, no Firestore emulator
- method: static code reading + hermetic repros via `TestClient` with `GEMINI_API_KEY=""` / `FIRESTORE_EMULATOR_HOST=""` / temp `NINE_DATA_DIR`; every finding below was reproduced on the current working tree
- prior-round parity checked against TRACKER.md/LEDGER.md + torture-1..27 reports (no re-files: 422 status enum t21-f5, 502 contract t18-f5/t14-f10/t15-f12, body cap t7-f6, node-timeout fail-fast t22-f2, server-routed events t20-f3, Firestore fallback t14-f10/t15-f13/t24-f2, corrupt-JSONL tolerance t4-f1/t8-f7, events-store construction guard t15-f12/t16-f7, per-request-Firestore-retry latch t15-f13)

## FINDING 1
- area: runtime (deploy/server.py `_LazyFallbackLedger`)
- severity: high
- title: A legitimate 404 (`LedgerError` from a missing job) latches `_ledger_failed=True` and silently swaps the whole API onto the LOCAL JSONL ledger — Cloud Run loses durable state visibility and every later write diverges from Firestore
- evidence: `deploy/server.py:254-284` — the `__getattr__` wrapper is `except Exception as exc:` (line 258) with NO `LedgerError` carve-out for the PRIMARY. The comment at line 280-283 ("A LedgerError from a fallback QUERY ... is legitimate and propagates") only covers the *fallback* query — the primary's own 404 (a completely normal outcome of `GET /v1/jobs/{unknown}` or a stale job id in a chain) is treated as an outage: warning printed, `JSONLLedger(LEDGER_PATH)` constructed, `_ledger_failed = True` (line 279). Repro (`python -c`, FakePrimary that raises `LedgerError("job not found: nope")`): `_ledger_failed` flips True after ONE 404; `get_ledger()` thereafter returns a plain `JSONLLedger` (asserted `type(...).__name__ == "JSONLLedger"`).
- impact: on Cloud Run the production backend is Firestore; any single miss (404, or a `get()` on a job id another instance wrote a moment ago) permanently degrades the API to local JSONL — jobs submitted afterwards are invisible to the rest of the fleet and die with the container. `/health` stays 200. This is the t15-F13 latch applied to the WRONG trigger.
- suggested_fix: in `__getattr__`'s wrapper, catch `LedgerError` from the primary and re-raise immediately (it is the endpoint's 404 contract); only fall through to JSONL for non-`LedgerError` exceptions (gRPC/network/`Unknown`). Regression test: FakePrimary raising `LedgerError` → `ledger.get("nope")` raises 404 AND `_ledger_failed` stays False; FakePrimary raising `RuntimeError` → fallback engages + latch.
- effort: S

## FINDING 2
- area: gates (nine/gates/evidence.py)
- severity: high
- title: Gate check errors degrade to "FIX" with a raw exception string — a broken check (TypeError/KeyError inside a check) is reported as a *failed verification* that the evidence itself can never fix, so a job stays `fixing`/`awaiting_evidence` forever instead of failing loud with the real cause
- evidence: `nine/gates/evidence.py:2086-2094` — `except Exception as exc: results[name] = {"passed": False, "message": f"check error: {exc}"}`. The executor only sees `passed: False` → verdict FIX → retry loop re-runs the same model node (re-burning budget) with a "fix_directive" that names a broken gate, not the artifact. A check that raises on every attempt (e.g. `ctx.get("node_exit_codes")` mis-typed, a bad closure) can never pass, so the job is stuck until max_fix_loops then `blocked` — indistinguishable from genuinely failing evidence.
- impact: model budget burned in doomed fix loops; operator confusion (BLOCKED job whose evidence is actually fine); a check bug takes down every job routed through that lane.
- suggested_fix: mark check *errors* as a distinct outcome — e.g. `{"passed": False, "message": ..., "error": True}` and have the executor treat a gate-check exception as a hard BLOCK/fail-loud (like WorkflowError) rather than a FIX directive. Regression test: register a check that raises; assert verdict is not FIX and no re-run is attempted.
- effort: M

## FINDING 3
- area: robustness (deploy/server.py + nine/learn/learner.py)
- severity: medium
- title: `RouteEventStore.all()`/`CandidateStore.all()` read the store with an unguarded `read_text` — a directory (or unreadable FIFO) at events.jsonl turns `GET /v1/events` and `GET /v1/stats` into a raw `IsADirectoryError` HTTP 500, violating the documented clean-502 contract
- evidence: `nine/learn/learner.py:94` `for line in self.path.read_text(...)` and `:138` (candidates) — no OSError wrap, while the write path (`record`, line 84-90) got the t21-F1 belt. Repro: mkdir at `EVENTS_PATH` then `GET /v1/events` → TestClient raises `IsADirectoryError: [Errno 21]` (traceback through `server.py:603`), i.e. an HTTP 500 if the server survived; same for `/v1/stats` through `learner.cands.all()` (`learner.py:138`). t15-F12 wrapped only *construction* (`get_learner`), not the read side.
- impact: a single filesystem accident (or a prior process touching the path) breaks the LEARN loop observability endpoints with an opaque 500; monitoring/automation keying on `/v1/events` gets a crash instead of a clean error.
- suggested_fix: wrap both `all()` reads in try/OSError → return `[]` + a printed warning (best-effort read, matching the write path's t21-F1 belt), and/or raise `LedgerUnavailable` from `get_learner()` consumers so the server 502s cleanly. Regression test: directory at events.jsonl → `/v1/events` returns 200 `{"count": 0}` (or clean 502), never 500.
- effort: S

## FINDING 4
- area: runtime (deploy/server.py events endpoint)
- severity: medium
- title: `GET /v1/events?limit=0` and `limit=-N` are accepted (200) and return *all* events (or the last N) instead of being rejected — `all_ev[-limit:]` makes `limit=0` the unbounded case and `limit=-1` a silent "last 1", breaking the documented "limit" contract for automation
- evidence: `deploy/server.py:599-607` — `events(limit: int = 50)` slices `all_ev[-limit:]` with no `ge=1` constraint. Repro (3 events seeded + 1 real): `?limit=0` → 200 with **4** events (`count: 4`, `events: 4` — full store leak), `?limit=-1` → 200 with 3, `?limit=1` → 1, `?limit=abc` → 422. The README/health contract for automation is "recent N events"; `limit=0` silently returns the entire (potentially unbounded) store and `limit=-1` returns nearly everything — both are data-exposure/amplification bugs for a monitoring consumer.
- impact: an automation that probes with `limit=0` (common "no limit" idiom) pulls the whole LEARN store per poll; on a long-lived deployment that is unbounded memory/bandwidth. Negative limits silently invert intent.
- suggested_fix: `limit: int = Field(50, ge=1, le=1000)` (or explicit `if limit < 1: raise HTTPException(422, ...)`); regression test: `limit=0`, `limit=-1`, `limit=1001` → 422; `limit=50` unchanged.
- effort: S

## FINDING 5
- area: router (deploy/server.py SubmitRequest + nine/router/classifier.py)
- severity: medium
- title: Whitespace-only tasks pass `min_length=1` and route to the `respond` lane — a model call is made with a blank prompt, and with a live backend the job SHIPs "evidence" derived from an empty task (no `.strip()` anywhere on the submit path)
- evidence: `deploy/server.py:289-291` `task: str = Field(..., min_length=1, max_length=2000)` — no strip; `submit()` line 404 uses `payload.task` as-is; `KeywordRouter.classify` (classifier.py:139-156) lowercases but does not strip, so `"   "` hits no keyword → universal `respond` fallback (line 152-155). Repro: `POST /v1/submit {"task": "   "}` with faked responder → 200 `status: shipped`, `decision.workflow_id: "respond"`, SHIP verdict on RESPONSE.md written from `respond_text("   ")` (responder.py `_respond_run` writes `task = str(inputs.get("task",""))` → model call). Without fakes it is a 502 "respond requires an LLM key" — i.e. it spends the model call either way.
- impact: paid model inference on blank input; ledger/events polluted with SHIP entries whose task_redacted is empty; violates the "every prompt is a real task" doctrine (t21-F5 sibling on the API side).
- suggested_fix: `task = payload.task.strip()` after validation (or a validator that rejects `not task.strip()` with 422). Regression test: `{"task": "   "}`, `{"task": "\n\t"}` → 422; `{"task": "hi"}` unchanged.
- effort: S

## FINDING 6
- area: runtime (deploy/server.py `_guard` / `_check_rate_limit`)
- severity: medium
- title: The per-IP rate limiter keys on `request.client.host` — behind Cloud Run's HTTPS load balancer every request carries the LB's IP, so the global 30/60s budget collapses to one shared bucket for ALL clients (a single noisy tenant DoSes the whole API, and the "per-IP" guarantee in the docstring is false in production)
- evidence: `deploy/server.py:327` `ip = request.client.host if request.client else "unknown"` — Cloud Run terminates TLS at the frontend and always presents the egress LB IP (X-Forwarded-For carries the real client, which the code never reads). Repro of the mechanics: 35 rapid `GET /v1/events` → codes `{200: 30, 429: 5}` — the shared bucket works; on Cloud Run that bucket is *global*, so any tenant exceeding 30 req/min blocks everyone (no per-IP isolation), and scanners hitting "unknown" share one queue.
- impact: availability — one consumer can 429 the entire service; the documented per-IP semantics are not what ships; also `X-Forwarded-For` spoofing would be the naive fix (must trust only the platform header, e.g. `X-Forwarded-For` last hop from the LB or `X-Envoy-External-Address`).
- suggested_fix: key on the real client IP when a trusted platform header exists (Cloud Run: last value of `X-Forwarded-For` as inserted by the LB, or `X-Envoy-External-Address`), falling back to `client.host` locally; document the trust boundary. Regression test: middleware test injecting X-Forwarded-For with two distinct values → each gets its own 30-budget.
- effort: M

## FINDING 7
- area: runtime (deploy/server.py module import)
- severity: low
- title: `MODEL` is captured once at import time (`MODEL = llm_provider.model_name()`) — a container that starts with no LLM key (or before the secret is mounted) reports `model: "none"` in `/health` forever, and a key injected later never updates it (stale config surface, t9-F5's "actual serving model" contract broken at deploy time)
- evidence: `deploy/server.py:156` `MODEL = llm_provider.model_name()` evaluated at import; `/health` (line 397-399) returns it verbatim; `llm_provider.model_name()` is a pure env read (verified: no caching), so the module-level constant is the only staleness — every other model-name read in the codebase is live. Repro (hermetic): import `deploy.server` with `GEMINI_API_KEY=""` → `/health` shows `model: "none"`; set `GEMINI_API_KEY=sk-x` in the same process → `/health` still `"none"`.
- impact: health/monitoring that keys on the model field mis-reports the serving model for the container's lifetime; on Cloud Run with a secret mounted after boot, liveness says "no model" while the API is fully operational (or the reverse after a key rotation).
- suggested_fix: make `/health` call `llm_provider.model_name()` live (drop the constant), or refresh `MODEL` lazily; regression test: mutate the env in-process → `/health` reflects the new model name.
- effort: S

## FINDING 8
- area: robustness (deploy/server.py `_LazyFallbackLedger` + nine/ledger/firestore_ledger.py)
- severity: low
- title: `FirestoreLedger._ref(job_id)` interpolates the job id into a document path with no escaping — ids containing `/`, unicode, or `.` (the API accepts any path segment and chains use uuid4, but recovery/migration tooling and console-edited ids can contain them) silently address a different/nested document or raise gRPC InvalidArgument, returning an opaque 500 instead of the clean 404 contract
- evidence: `deploy/server.py:591-596` `GET /v1/jobs/{job_id}` → `get_ledger().get(job_id)`; `firestore_ledger.py:41-42` `def _ref(self, job_id): return self.db.collection(self.collection).document(job_id)`. Firestore document ids must not contain `/` (path separator) — a nested-looking id like `a/b` resolves to a subpath and, worse, `".."`-style segments are silently normalized by the emulator/console. Hermetic JSONL repro: `GET /v1/jobs/a/b` and `GET /v1/jobs/..` both 404 cleanly on JSONL, but the Firestore path can raise `InvalidArgument` (4xx→ but surfaced as a raw 500 by the `except LedgerError` handler since Firestore raises google.api_core exceptions, not LedgerError) — the parity claim between the two backends is broken for odd ids.
- impact: automation that builds job ids from user input gets divergent error semantics across backends; worst case a `..`-style id writes/reads outside the intended doc namespace (document path injection), though Firestore validates the path first.
- suggested_fix: validate job_id shape at the endpoint (`^[A-Za-z0-9_-]{1,64}$` or uuid regex → 422 otherwise) and/or in `FirestoreLedger._ref` raise `LedgerError` (clean 404) for ids containing `/` or `.` segments; regression test: `GET /v1/jobs/..`, `a/b`, unicode → same clean 404 on both backends.
- effort: S

---

## Verified-holding surfaces (checked, not re-filed)
- 413 body cap: content-length fast path + chunked read cap both hold (server.py:45-116, guard 344-360) — chunked bodies over 1 MiB get 413 before buffering.
- NINE_NODE_TIMEOUT_S=0/-N fail-fast 400 pre-commit (server.py:422-432) — verified live in code; prior t22-F2.
- `/v1/jobs?status=` 422 enum (server.py:584-587) — verified; t21-F5.
- Corrupt JSONL lines in ledger/events/memory are skipped everywhere (t4-F1/t8-F7) — confirmed in `_looks_like_job` + `errors="replace"` reads.
- Firestore→JSONL fallback on genuine outage latches once (t15-F13) — confirmed; the 404-trigger case is the NEW Finding 1.
- `_record_route_event` CANCELLED skip + OSError belt (server.py:543-576) — confirmed holding (t18-F2/t21-F1).
- Weird job ids over JSONL backend: `../etc/passwd`, `a/b/c`, `..`, 300-char, `a b` all return clean 404 — JSONL side safe (Finding 8 is Firestore-parity only).

## Method / constraints honored
- static exploration first, report written before deeper verification
- hermetic only: `TestClient` with empty keys + temp `NINE_DATA_DIR`; no git, no real ADK/model/Firestore nodes touched
- no repo files modified other than this report
