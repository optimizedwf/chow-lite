# TORTURE-21 — runtime + gates adversarial audit (adk_runtime / evidence gate / cancel-race / redaction / ledger parity)

**Repo**: chow-lite @ `789f875` (slice 42 — "HARDEN doc-truth ... round-11 torture spawned (torture-21 runtime+gates)"). HEAD verified; working tree has `nine/chains/flagship.py` + `nine/workflows/debug_wf.py` MID-EDIT — both excluded from every finding below.
**Scope**: `nine/runtime/adk_runtime.py`, `nine/gates/evidence.py`, `nine/runtime/workflows.py` (gate window + executor transitions), `nine/router/classifier.py` (`redact`), `nine/ledger/ledger.py` + `nine/ledger/firestore_ledger.py`, `nine/cli.py`, `deploy/server.py`, `nine/chains/chain.py`, `nine/runtime/llm_provider.py`, `nine/runtime/fsafety.py`, `nine/learn/learner.py`, `nine/memory/graph.py`, `nine/registry.py`.
**Method**: static + hermetic repros only (zero Gemini quota — no live model calls, no network, no keys). All repros run with `.venv/bin/python` (3.12.13) against the real modules with scratch dirs under /tmp. READ-ONLY: no repo file modified by this worker except this report.
**Test baseline**: `pytest --collect-only -q` → **486 collected** (HEAD slice-42 count).

**Holding surfaces re-verified (NOT re-filed)**: `asyncio.run` inside `ADKAgentNode` is safe — server endpoints are sync `def` (threadpool), never a live event loop; adk_runtime's `last_exc if not events` logic is correct (events can only be non-empty from a successful attempt; final empty-stream with a prior exception raises; 3x empty stream → `RuntimeError` fail-loud, never a silent pass); `_check_rate_limit` sweep bounds `_hits` to ~unique-IPs-per-window (torture-16 F6 hold); `eval_json_check` rejects empty/`[]` checks and non-literal `true`; `fsafety.contained_write` resolves + `is_relative_to` correctly; `_record_route_event` CANCELLED-skip present on BOTH CLI and server (t20 F3 parity); CLI `discover` status enum validation landed (t20 F6) — but the SERVER did not get it (finding 5).

6 findings: 3 medium, 3 low.

---

## FINDING 1
- area: evidence gate (runtime) — `nine/gates/evidence.py` + `nine/runtime/workflows.py`
- severity: medium
- title: `gate.evaluate()` has no timeout and `load_eval_json`/check reads have no regular-file guard — a FIFO at `EVAL.json` (or `VERIFIED.json`/`CHECKS.json`) hangs the job in `awaiting_evidence` forever and wedges the submit process/thread; on the server each hang permanently consumes a threadpool worker (default 40 → repeatable DoS)
- evidence:
  - `nine/gates/evidence.py` `load_eval_json`: `if p.exists() and not p.is_symlink(): data = json.loads(p.read_text())` — `read_text()` on a FIFO blocks until a writer appears; no timeout, no `is_file()` check. Same unbounded `read_text` in `_verified_json_check` (`VERIFIED.json`) and `_honesty_check` (`CHECKS.json`) in `nine/workflows/verify_wf.py`. `eval_json_check` is registered on the build/test/review/research-deep/etc. lanes (`resolve_gate`).
  - `nine/runtime/workflows.py` `execute()`: every NODE is bounded (`Node.timeout_seconds`, default 300, `NINE_NODE_TIMEOUT_S`) but `gate.evaluate(...)` is a plain synchronous call — the gate is the only unbounded read in the pipeline. A FIX verdict re-runs the DAG but the gate hang happens before any verdict.
  - Reachability: workflow self-test nodes execute model-written artifacts (build's `_build_self_test_command` runs `python3 solution.py` / pytest; test/verify/refactor lanes similar), so a task can steer the model to leave a FIFO at `EVAL.json` (e.g. `os.mkfifo("EVAL.json")` swapped in by a background Popen after the self-test writes), or a local operator can `mkfifo work/<job>/EVAL.json` directly. The gate then never returns.
- repro (hermetic; gate hung >4s and was killed):
  ```
  TMP=$(mktemp -d); mkfifo "$TMP/EVAL.json"
  # probe: EvidenceGate(eval_json_check + exit_codes_check).evaluate({}, TMP)
  timeout 4 .venv/bin/python probe.py "$TMP"   # exit 124 — gate still blocked on read
  ```
- impact: job stuck `awaiting_evidence` forever; `nine submit` CLI process hangs; POST /v1/submit hangs a threadpool worker permanently — 40 FIFO submissions exhaust the pool and the whole API wedges (no gate timeout, no connection abort recovery); `nine cancel` fixes the durable state but the hung process never exits.
- suggested_fix: (a) wrap `gate.evaluate` in a timeout like nodes (e.g. `NINE_GATE_TIMEOUT_S`, default 60, raise/verdict BLOCK on expiry), and/or (b) require `p.is_file()` (or `os.stat` S_ISREG) in every gate disk read so FIFOs/devices/sockets are treated as missing evidence.
- effort: low (one timeout wrapper + one `is_file()` guard in `load_eval_json` and the two verify checks).

## FINDING 2
- area: executor cancel race — `nine/runtime/workflows.py`
- severity: medium
- title: an operator `nine cancel` arriving during the gate window is silently overwritten — the job durably ends `shipped` (or `blocked`) with a SHIP route event recorded to the LEARN loop, last-line-wins
- evidence:
  - `nine/runtime/workflows.py` `execute()`: `_cancelled()` is polled before the gate (`_cancelled(job)` then `job.transition("awaiting_evidence")` + `ledger.update`), but after `gate.evaluate` the executor unconditionally stamps a terminal transition (`shipped`/`blocked`/`fixing`) without re-checking. JSONLLedger is append-only last-line-wins, so the executor's terminal line silently supersedes the cancel line.
  - torture-8 F3 fixed the node-run cancel window; the gate window (gate duration, unbounded — see FINDING 1) is still uncovered.
- repro (hermetic, real JSONLLedger + WorkflowExecutor, 2.5s gate check):
  ```
  statuses in order: submitted, routing, running, awaiting_evidence, cancelled, shipped, shipped
  final durable status: shipped      # operator cancel DURABLY lost
  executor verdict: SHIP             # and a SHIP route event was recorded
  ```
  (Same result with a BLOCK verdict: `... awaiting_evidence, cancelled, blocked, blocked`.)
- impact: cancelled jobs durably SHIP and are fed to the LEARN loop as successes; the operator's cancel is silently lost; `nine cancel` prints "cancelled" while the job later shows shipped — misleading durable audit state.
- suggested_fix: re-check `_cancelled()` immediately after `gate.evaluate` and again before each terminal `ledger.update`; if cancelled, transition to `cancelled` (no verdict stamp, no route event) and return.
- effort: low.

## FINDING 3
- area: redaction — `nine/router/classifier.py` (`redact`)
- severity: medium
- title: `redact()` misses modern credential families: GitHub fine-grained PATs (`github_pat_…`), GitLab PATs (`glpat-…`), Slack webhook URLs (`hooks.slack.com/services/…`), AWS STS session keys (`ASIA…`), Linear API keys (`lin_api_…`) — all pass through verbatim into the durable ledger, Firestore, route-event store, and memory summaries
- evidence:
  - The `gh[po]` token pattern (`((?:sk|pk|gh[po]))(?![a-z])…`) matches `ghp_`/`gho_` but NOT `github_pat_` (the `(?![a-z])` lookahead fails on the `i`), and nothing covers `glpat-`, `hooks.slack.com/services/`, `ASIA`-prefixed keys, or `lin_api_`.
- repro (hermetic, direct `redact()` calls — output equals input, i.e. zero redaction):
  ```
  my github_pat_11ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef token                       -> unchanged
  glpat-ABCDEFGHIJKLMNOPQRSTUVWX                                              -> unchanged
  https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXX   -> unchanged
  ASIA1234567890ABCDEFG                                                       -> unchanged
  lin_api_1234567890abcdef                                                    -> unchanged
  # contrast — covered families: gho_.. -> gho***, xoxb-.. -> xox***,
  #   sk-proj-.. -> sk***, api_key=sk-.. -> api_key=*** (t16 F4 / t20 F6 hold)
  ```
  Flow: `Router.classify` → `RouteDecision.task_redacted` → `ledger.update(job)` (JSONL + Firestore) → `_record_route_event(task_redacted[:200])` (CLI + server) → chain `_save_memory(task_redacted)` — the raw secret is persisted in every durable store.
- impact: a task that mentions a fine-grained PAT / GitLab token / Slack webhook / AWS STS key stores the live secret unredacted in the job ledger, the events store, and (on Cloud Run) Firestore — exactly the surfaces the redaction contract exists to protect; prior findings in this family (T16-F4, T20-F6) show these token shapes ARE in scope.
- suggested_fix: extend `_KEY`/pattern coverage: `github_pat_[A-Za-z0-9_]+`, `glpat-[A-Za-z0-9_-]+`, `hooks\.slack\.com/services/[A-Z0-9]+/[A-Z0-9]+/[A-Za-z0-9]+`, `ASIA[0-9A-Z]{16}`, `lin_api_[A-Za-z0-9]+` (mirroring the existing `sk-`/`ghp-`/`xox` handling), plus unit tests for each family.
- effort: low.

## FINDING 4
- area: runtime config — `nine/runtime/adk_runtime.py`
- severity: low
- title: `NINE_MAX_LLM_CALLS` accepts `0`/negative values — the ADK per-run LLM budget is silently DISABLED ("no enforcement… never ending communication between the model and the agent"), the exact failure the env var exists to prevent
- evidence:
  - `adk_runtime.py`: `try: _max_calls = int(os.environ.get("NINE_MAX_LLM_CALLS", "24")) except ValueError: _max_calls = 24` — only malformed strings fall back; `-1`/`0` pass through into `RunConfig(max_llm_calls=_max_calls)`.
- repro (hermetic, no model calls — adk 2.6.x installed in the venv):
  ```
  RunConfig(max_llm_calls=0)  -> constructs fine
  RunConfig(max_llm_calls=-1) -> constructs fine
  google-adk itself logs: "max_llm_calls is less than or equal to 0. This will
  result in no enforcement on total number of llm calls … never ending
  communication between the model and the agent in certain cases."
  env NINE_MAX_LLM_CALLS=-1  -> parsed -1 (int() succeeds)
  ```
- impact: an operator typo (`NINE_MAX_LLM_CALLS=0` in the deployment env) silently disables the tool-loop budget on every ADK node — the exact runaway-spend scenario the `LlmCallsLimitExceededError` path (fail loud) is meant to guard; only the node timeout remains as a backstop.
- suggested_fix: range-validate after parse (`if _max_calls < 1: warn loud + use 24`), consistent with the `Node.timeout_seconds` T8-F4-style validation already in the codebase.
- effort: trivial.

## FINDING 5
- area: server API parity — `deploy/server.py`
- severity: low
- title: `GET /v1/jobs?status=…` silently accepts any status string and returns `200 {"jobs": []}`, while the CLI got the enum validation in t20 F6 — a typo is indistinguishable from an empty ledger over the API
- evidence:
  - `nine/cli.py` `cmd_discover` (t20 F6): `if args.status not in VALID_STATUSES: print(clean error); return 1`.
  - `deploy/server.py` `jobs()`: `return {"jobs": [j.to_dict() for j in get_ledger().discover(status=status)]}` — status is passed straight through; `JSONLLedger.discover` exact-matches and Firestore `where("status","==",…)` returns empty — both silently `200`.
- repro (static; code paths above; no server boot needed): `GET /v1/jobs?status=shippd` → `200 {"jobs": []}` (should be 422/400 "unknown status").
- impact: automation/operators cannot distinguish a status typo from a genuinely empty ledger over the API; monitoring misreads and the CLI/API contracts diverge (the same divergence class t20 F6 closed for the CLI).
- suggested_fix: validate `status` against `VALID_STATUSES` in the server handler → `HTTPException(422)` with the valid list, mirroring `cmd_discover`.
- effort: trivial.

## FINDING 6
- area: ledger parity (Firestore) — `nine/ledger/firestore_ledger.py`
- severity: low
- title: `FirestoreLedger.get()`/`discover()` run no shape guard — a doc missing `workflow_id`/`job_id`/`created_at` raises raw `KeyError`/`AttributeError` (HTTP 500 on `/v1/jobs/{id}`), while the JSONL ledger tolerates corrupt lines with a clean `LedgerError` → 404
- evidence:
  - `firestore_ledger.py` `get()`: `job = Job(workflow_id=rec["workflow_id"], job_id=rec["job_id"])` — unguarded subscript on `doc.to_dict() or {}`; `discover()` sorts by `job.created_at` (AttributeError on a doc without `created_at`); `transition`/`status`/`artifacts` all funnel through `get()`.
  - `nine/ledger/ledger.py` JSONLLedger `_looks_like_job` + per-line try/skip: corrupt lines degrade to `LedgerError("job not found")` → clean 404 on the API and clean CLI errors.
  - Reachability: Firestore docs are console-editable and version-driftable (older writes, partial manual edits, docs created out-of-band); JSONL/Firestore parity is a documented contract (t18 F1 family).
- impact: one malformed doc turns `GET /v1/jobs/{id}` (and `nine status` against a Firestore ledger) from a clean 404 into a raw 500/KeyError traceback; `discover()` 500s on the same doc.
- suggested_fix: apply the JSONL shape guard (or `validate("agent-job", rec)` tolerant-load) in `get()`/`discover()` before constructing the `Job`; skip or clean-error malformed docs the way the JSONL loader does.
- effort: low.

---

**Re-verified non-findings (held)**: `asyncio.run` in `ADKAgentNode._ensure_session` runs on sync `def` endpoints (threadpool) and CLI threads — no live-loop crash; adk_runtime empty-stream/`last_exc` handling is correct and fail-loud; rate-limiter idle sweep bounds `_hits`; `eval_json_check` rejects `checks: []` and non-literal `true`; directory-named artifacts (RESPONSE.md/EVAL.json as dirs) are caught by the stale guard → BLOCK, and node-level writes to a FIFO are caught by node timeouts (only the gate read is unbounded — FINDING 1); `fsafety.contained_write` prevents job-dir escapes; CANCELLED route-event skip present CLI+server.
