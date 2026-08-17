# Torture-Tester 36 — learn + memory + deploy re-attack

> **HARVESTED slice-54: ALL 8 FINDINGS FIXED** (T36-F1..F8).
> Regression tests: tests/test_torture_harvest_17.py (12 tests).
> See LEDGER.md round-18 for dispositions.

Worker: torture-36 (DS4 Flash fleet)
Surface: learn + memory + deploy/server — FRESH angles (round-17 T31/T32/T33
filed empty; T34's two store-shape findings are FIXED in slice-52 and NOT re-filed).
Method: static source analysis + hermetic repros via `.venv/bin/python`
(no Gemini quota; no real ADK calls — ADKAgentNode repro stubs the runner).
All traces reproduced below.

Checklist of invariants probed:
- no aux-write/read path may crash on corrupt/wrong-shape/unwritable stores
- no error path may echo the RAW task (credentials) into logs/HTTP responses
- LEARN idempotence & event-identity must survive truncation/prefix collisions
- server: 4xx semantics, CORS, rate-limit trust boundary, degraded-store behavior
- docs claims must be true of code

## FINDING 1

- area: runtime (ADK error path) / robustness
- severity: high
- title: ADK error messages embed the RAW task (up to 120 chars) — credentials that survive `redact()` leak into CLI stderr AND HTTP 502 responses
- evidence:
  - nine/runtime/adk_runtime.py:213-216 `RuntimeError(f"ADK agent exceeded max_llm_calls={_max_calls} for task: {task[:120]!r} ...")` — RAW `task`, never `redact()`ed
  - nine/runtime/adk_runtime.py:252-255 `RuntimeError(f"ADK agent produced no output for task: {task[:120]!r} ...")` — same
  - Reproduced hermetically (stubbed runner, empty stream): task `"deploy api_key=sk-ABCDEF123456 to prod"` -> exception message contains the full `api_key=sk-ABCDEF123456` verbatim.
  - Why redact() doesn't save it: `redact("deploy api_key=sk-ABCDEF123456 to prod")` -> `"deploy api_key=*** to prod"` (that case is caught), but `redact("use AIzaSyD1234567890abcdef as key")` -> `"use AIza*** as key"` and `redact("change the password to hunter2")` -> `"change the password=*** hunter2"` — several REAL task shapes leak the tail. Redaction is lexical and lossy; the error path must not depend on it.
  - Propagation: WorkflowError/ChainError bubble the message to cli.py `print(f"[error] job {job.job_id} failed loud: {exc}")` (nine/cli.py:1058) and server.py's `WorkflowError`/`ChainError` handlers -> JSONResponse 502 with `str(exc)` (deploy/server.py:52-73); chain.py:206-207 `raise ChainError(f"hop {hop.id} crashed: {exc}")`.
- impact: any task containing a credential (or PII) that hits an empty stream / budget exhaustion — the NORMAL Gemini-free-tier failure — gets the secret echoed into server logs (Cloud Run) and back to the API client. The router redacts at the ledger boundary, but the ERROR path is unredacted.
- suggested_fix: `task = redact(str(inputs.get("task", "")))` before embedding in either RuntimeError (import `redact` from nine.router.classifier). Regression test: fake empty-stream node with a task containing a credential-shaped tail (`"use AIzaSyD1234567890abcdef as key"`), assert the raised message contains no value fragment.
- effort: S

## FINDING 2

- area: learn
- severity: medium
- title: route-event identity truncates to `job_id[:8]` — distinct job ids with a shared 8-char prefix collapse into ONE event, so LEARN permanently blindfolds itself on those runs
- evidence:
  - nine/cli.py:451-452, deploy/server.py:579, nine/chains/chain.py:251: `event_id=f"ev-{job.job_id[:8]}-{run_seq}"` / `ev-{hop_job.job_id[:8]}-...`
  - Reproduced hermetically: two distinct jobs `abcdef1234567890` and `abcdef12AAAAAAA` (same `[:8]` = `abcdef12`) with identical other fields -> `Learner.learn()` produced ONE candidate with evidence `["ev-abcdef12-0"]` instead of two; after apply/revert, both runs are permanently deduped (learner.py:279-283 `used_events` + `has()` evidence match), so verdict flips from either job are never re-observed.
  - T27-F1 (run_seq bump) fixed the SAME-JOB re-run case; the cross-job prefix collision is unfixed.
- impact: with UUIDv4 job ids a prefix collision needs ~2^32 jobs, BUT job ids are user-visible and the ledger/job-detail API accepts arbitrary ids — a real multi-tenant or imported ledger with colliding prefixes silently loses LEARN visibility for those jobs' entire lifecycle.
- suggested_fix: use the full job_id (or hash it) in the event id — no `[:8]`; T27-F1's `run_seq` suffix already guarantees same-job uniqueness. Regression test: two events with colliding 8-char prefixes but distinct full job ids must yield two candidates and survive apply/revert without dedupe.
- effort: S

## FINDING 3

- area: learn (apply/revert)
- severity: medium
- title: `nine learn apply`/`revert` raw-crash AttributeError on a valid-JSON wrong-shape `params` in a candidate record (the same class of corruption T30-F1/T34-F1 fixed for events/status — but `params` is unguarded)
- evidence:
  - nine/cli.py:799-800 and 888-889: `cand.params.get("workflow_id", "")` / `cand.params.get("keyword", "")` — no isinstance-dict guard
  - Reproduced: CandidateStore JSONL line `{"candidate_id": "cand-abc123", "kind": "keyword", ..., "params": "garbage-string"}` -> `cand.params.get(...)` raises `AttributeError: 'str' object has no attribute 'get'` (full traceback, exit 1). Also: a candidate line with `params` MISSING parses to `{}` and apply prints the confusing "not auto-applicable" line instead of a shape error.
  - CandidateStore.all() (nine/learn/learner.py:235-246) constructs `ImprovementCandidate(**rec)` with NO shape validation beyond TypeError — wrong-typed fields flow straight through.
- impact: the human-approval queue is the LEARN loop's write path; one hand-edited/version-skewed candidate bricks `nine learn apply`/`revert` with a raw traceback (the same UX the ledger/store read paths were hardened to avoid).
- suggested_fix: `_coerce_candidate` guard (params must be dict-or-missing, description/evidence str/list) in CandidateStore.all(), mirroring `_coerce_route_event`; apply/revert refuse cleanly ("candidate record corrupt: params must be an object"). Regression test: wrong-shape params line -> `learn apply` prints one clean line, exit != 0, no traceback.
- effort: S

## FINDING 4

- area: memory
- severity: low/medium
- title: LocalMemoryGraph memory_id is deterministic and prefix-truncated — distinct jobs/artifacts with a shared `job_id[:8]` + artifact basename write DIFFERENT records under the SAME document id (search returns duplicates; Firestore parity breaks)
- evidence:
  - nine/memory/graph.py:77 `memory_id = f"mem-{job_id[:8]}-{artifact_name.split('.')[0]}"` (LocalMemoryGraph only; FirestoreMemoryGraph uses `uuid4` — backends disagree)
  - Reproduced: `save_artifact_summary(job_id="job-abcdef12-1", artifact_name="HANDOFF.md")` and `(job_id="job-abcdef12-2", artifact_name="HANDOFF.md")` both return `mem-job-abcd-HANDOFF`. Subdir artifacts produce path fragments: `artifact_name="solution/main.py"` -> id `mem-job-abcd-solution/main` (slash in an id).
- impact: duplicate memory entries masquerade as one document; any id-keyed consumer (dedupe, deletion, Firestore migration) collides. Deterministic ids were presumably chosen for stability, but the 8-char prefix guarantees collision at far lower scale than the event id (same prefix, same artifact name).
- suggested_fix: include the full job_id hash + hop/chain in the id, or use uuid4 like Firestore (parity). Regression test: two save calls with colliding prefixes return distinct ids and distinct records.
- effort: S

## FINDING 5

- area: learn
- severity: low
- title: BLOCK candidates embed the RAW `fix_directive` into the durable candidate description — fix_directive is never redact()ed anywhere on the write path
- evidence:
  - nine/learn/learner.py:314-316: `f"workflow '{ev.workflow_id}' BLOCKed with fix_directive '{ev.fix_directive[:80]}'; consider a stricter gate..."` — description is the candidate's durable text
  - nine/chains/chain.py:264: `fix_directive=inputs.get("fix_directive", "")` — raw, and chain.py's only redact() calls are on task_redacted/summaries, not fix_directive
  - Reproduced: event with `fix_directive: "api_key=sk-ABCDEF123456 leaked"` -> candidate description contains the full string; `_suggest` (learner.py:273-282) skips dedupe on identical text, so it lands in candidates.jsonl and in `nine learn candidates` / GET /v1/stats output.
- impact: a model or workflow that echoes a credential into a gate-failure directive (plausible — FIX directives quote gate messages) persists it in a second durable store (candidates.jsonl) that the ledger-redaction boundary never touches. The task text is redacted; the directive is not.
- suggested_fix: `redact(ev.fix_directive[:80])` before embedding (import from nine.router.classifier). Regression test: BLOCK event with credential-shaped directive -> candidate description contains no value.
- effort: S

## FINDING 6

- area: deploy/server
- severity: low
- title: rate limiter trusts client-supplied X-Forwarded-For in non-Cloud-Run deployments — XFF rotation defeats the per-IP limiter and a shared middlebox IP DoSes all tenants
- evidence:
  - deploy/server.py:349-356: `ip = hops[-1]` unconditionally, with NO hop-count/trust check; comment claims "only the last hop is trusted (never a client-supplied first value)" — true on Cloud Run (LB appends), false anywhere the header is client-writable (local demo, docker, bare metal).
  - Reproduced with TestClient (NINE_DATA_DIR=tmp): (a) 60 GETs with rotating XFF last hops `9.9.9.{i%250}` -> 0 × 429; (b) 35 GETs from tenant A `10.0.0.1` then 1 GET from tenant B `10.0.0.2` -> B gets 200 (A exhausted the shared 30/60s bucket — the exact collapse T28-F6 fixed for the Cloud Run case is still live for every non-LB deployment, because the limiter keys on the LAST hop which the client controls there).
  - Also: OPTIONS preflight returns 405 with no Access-Control-* headers (no CORSMiddleware anywhere; confirmed `grep -i cors` over deploy/ and README) — browser dashboards cannot call /v1/*, and the API accepts any Origin silently when it does.
- impact: on any self-hosted/demo deployment the per-IP rate limit is cosmetic (rotate XFF), and behind a shared NAT one tenant's 30 req/60s starves everyone. Combined with no CORS, the documented "API surface" is only curl-usable.
- suggested_fix: gate XFF trust on `K_SERVICE`/a NINE_TRUST_PROXY flag (only then use the last hop; otherwise client.host), and add CORSMiddleware with an allowlist (or explicit 403 for disallowed Origins) — a security-conscious default. Regression tests: rotating XFF cannot bypass when trust flag unset; OPTIONS preflight returns proper CORS headers.
- effort: M

## FINDING 7

- area: docs
- severity: low
- title: `_derive_keyword` mangles non-ASCII tasks into non-word keywords ("déployer" -> "ployer") — the keyword-candidate params can seed the router catalog with garbage
- evidence:
  - nine/learn/learner.py:339: `re.findall(r"[a-z]{4,}", ev.task_redacted.lower())` — ASCII-only word class; "déployer café ☕" -> token "ployer" (drops the é, then takes the 4+ tail)
  - Reproduced: event task_redacted `"déployer café ☕"` -> candidate params `{"keyword": "ployer", ...}`. An operator who applies it via `nine learn apply` (cli.py:799-820 path) adds `"ployer"` to the router catalog — a non-word that no future task contains, so the learned keyword is dead weight and pollutes catalog.json.
- impact: `nine learn apply` is a human-approved, regression-gated operation — but the gate only proves routing tests still pass; it cannot catch a semantically useless keyword. Non-ASCII tasks (common in operator text) degrade LEARN's output quality silently.
- suggested_fix: skip non-ASCII tasks in _derive_keyword (require token.isalpha() or a Unicode-aware `\w` + ascii check, or return "" so the candidate says `<human-chosen>`) — never emit a mangled token. Regression test: déployer/日本語 tasks -> keyword "" or a clean token.
- effort: S

## FINDING 8

- area: learn (idempotence)
- severity: low
- title: scan idempotence is evidence-string-based and case/whitespace-sensitive — semantically identical events (only case/whitespace drift) re-suggest candidates after apply
- evidence:
  - nine/learn/learner.py:251-253 `has()`: `c.description == description and c.evidence == evidence` — exact string equality; learner.py:280-283 `used_events` uses event ids, so the event-level guard works, but `has()` is the dedupe for NEW candidates
  - Reproduced: two BLOCK events whose `fix_directive` differs only in case/whitespace ("fix A" vs "fix  A") produce two candidates with descriptions differing only by that fragment (learner.py:314-316 embeds the raw directive) — `has()` misses, so apply->revert->rescan of the same underlying event re-suggests a near-duplicate.
  - (Same-string case is handled: my 3-event repro with 2 identical directives produced exactly 2 distinct candidates, confirming the belt works when strings are byte-identical.)
- impact: after the human approves one fix and reverts it, a rescan of the same BLOCK re-creates a look-alike candidate the human must reject again — noise in the approval queue, and apply/revert churn on catalog.json.
- suggested_fix: normalize descriptions/evidence in `has()` (casefold + collapse whitespace) and/or make dedupe event-id-keyed rather than description-keyed. Regression test: two directives differing only by case/space -> one candidate.
- effort: S
