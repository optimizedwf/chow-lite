# TORTURE-TESTER-18 Report — slice-34 fix verification + new gaps (torture-18: server / learn / docs)

Worker: TORTURE-TESTER-18. Repo HEAD: `0c92623` (slice 34 — "round-8 torture harvest (torture-15/16)"). All repros hermetic (no Gemini, no network, no quota): `.venv/bin/python` scripts under `/tmp/t18*.py`, real modules + in-process fakes only. READ-ONLY: no repo file was modified by this worker; the only repo write is this report. Working-tree note: a CONCURRENT process (slice-35 / torture-17 fix application) was editing the tree DURING this review — at final `git status` the uncommitted set was bench/bench_nine.py, bench/state.json (pre-existing), nine/chains/flagship.py, nine/gates/evidence.py, nine/runtime/workflows.py, nine/schema_validation.py, nine/workflows/*.py, tests/test_torture_harvest_{3,5,7,8,9}.py. Every finding below cites files that concurrent work did NOT touch (deploy/server.py, nine/cli.py, nine/ledger/firestore_ledger.py, nine/router/classifier.py, schemas/*.json), and the two cited spots in the concurrently-edited nine/runtime/workflows.py (`_abort_cancelled` 259-290, `job_dir.mkdir` 440) were re-verified unchanged at report time.

Attack surfaces probed (8): (1) CANCELLED end-to-end server.py+ledger, (2) `_LazyFallbackLedger` latch (T15-F13), (3) get_learner/get_memory clean-502 surface (T15-F12), (4) mutator validation / additionalProperties (T16-F3), (5) `_git_commit` bool contract + double-run (T16-F9), (6) redact() rewrite regression (T16-F4/T15-F6), (7) deploy.sh public-unauthenticated refusal (T16-F5), (8) doc-truth (README/SUBMISSION test counts, bench state).

Verified-holding surfaces (not re-filed): fallback latch — `_LazyFallbackLedger.__getattr__` wrapper latches `_ledger_failed` on ANY failed call and `get_ledger()` returns plain JSONLLedger afterwards, Firestore call-count stays 1 (`tests/test_torture_harvest_9.py:393` passes; `_resolve()` is dead code; a fresh JSONLLedger per `get_ledger()` call after latch is perf-only, not correctness); clean-502 GET surface — `/v1/events` + `/v1/stats` raise `LedgerUnavailable` on a bad NINE_DATA_DIR (`tests/test_torture_harvest_9.py:378` passes); T16-F3 mutators reject missing-required-field boundary objects (name-only artifact, BOGUS verdict, null `decided_at`); T16-F4 redact word-boundary controls (`ski`/`task`/`ask`/`ghost` safe, `sk-`/`pk-live-`/`ghp_` caught, chained `==` caught, quoted-token tails caught); T16-F5 deploy.sh — `--no-allow-unauthenticated` valid, NINE_API_KEY secret create is idempotent, loud refusal branch works; T16-F9 first-run contract — a single `apply` with a failing `_git_commit` leaves the candidate `pending` and warns loudly (F4 below is the *retry* hole in that same fix); doc-truth counts — real suite is **431 passed / 5 skipped (436 collected)**, matching SUBMISSION.md's count claim (its "431/431" phrasing plus the 5 live-gated skips is sloppy but not false; README coverage badge claims 80% vs 76% measured — cosmetic, noted only).

8 findings: 1 high, 4 medium, 1 low-medium, 2 low.

---

## FINDING 1
- area: Firestore ledger boundary (T4-F4 redaction parity) — `nine/ledger/firestore_ledger.py`
- severity: high
- title: FirestoreLedger.submit persists RAW task secrets — the redact()/validate() boundary exists only on JSONLLedger, so Cloud Run (the production backend) writes AKIA/sk-/password values verbatim into Firestore
- evidence:
  - `nine/ledger/ledger.py:247-262` — `JSONLLedger.submit` redacts the task and `validate("agent-job", ...)`s it, with the T4-F4 comment claiming "every submit path — CLI submit, CLI chain, POST /v1/submit — stores the same redacted task".
  - `nine/ledger/firestore_ledger.py:38-42` — `FirestoreLedger.submit` is `job = Job(...); self._ref(job.job_id).set(job.to_dict())` — no `redact()`, no `validate()`; `update()` (75-77) is a raw `merge=True` set too.
  - `deploy/server.py:197-204` — `get_ledger()` prefers Firestore whenever `google.cloud.firestore` imports (Cloud Run); `deploy/deploy.sh` sets `FIRESTORE_COLLECTION=nine-jobs` and deploys the image, so `POST /v1/submit` → FirestoreLedger is the production path.
  - Repro `/tmp/t18_f1.py` (fake Firestore client, invented key values): task `"deploy to prod with key AKIAIOSFODNN7EXAMPLE and sk-live-... and password=hunter3"` → stored doc contains the FULL raw task (`redacted: False`); JSONLLedger stores it redacted (`True`); Firestore submit also accepted a bogus `workflow_id="nope"` that agent-job validation rejects.
- impact: every cloud submit writes task text — including any credentials the user pasted (the exact class of secret T4-F4/T16-F4 was built to stop) — in plaintext to Firestore; job records are also readable via the operator API, and Firestore is a separate security boundary from the (redacted) JSONL audit trail. The "every submit path" claim in the JSONLLedger comment is false.
- suggested_fix: mirror JSONLLedger.submit in FirestoreLedger.submit: `if input and isinstance(input.get("task"), str): input = dict(input); input["task"] = redact(input["task"])` then `validate("agent-job", job.to_dict())` before `.set()`; add `validate("agent-job", job.to_dict())` in `update()` too. Regression test: fake Firestore client + submit a task containing `AKIA…`/`sk-live-…` → stored doc must not contain the secrets; a bogus workflow_id must raise SchemaValidationError.
- effort: S

## FINDING 2
- area: server CANCELLED end-to-end (T16-F1 parity) — `deploy/server.py`
- severity: medium
- title: server `_record_route_event` lacks the CLI's CANCELLED skip → an operator-cancelled job makes POST /v1/submit raw-500 (SchemaValidationError uncaught)
- evidence:
  - `nine/cli.py:361-371` — CLI's `_record_route_event` early-returns on `verdict.get("verdict") == "CANCELLED"` ("CANCELLED is not a route-event verdict … the schema would reject it, raw-tracebacking submit/recover").
  - `deploy/server.py:473-492` — the server copy has NO such skip; `deploy/server.py:458` calls `_record_route_event(get_learner(), job, decision, result["verdict"])` after `ex.execute` on every WORKFLOWS run, including cancelled ones.
  - `schemas/route-event.schema.json` — `"verdict": {"enum": ["SHIP", "FIX", "BLOCK", "UNVERIFIED"]}` (no CANCELLED). `deploy/server.py` exception handlers cover only WorkflowError (:121) and LedgerUnavailable (:128); SchemaValidationError is unhandled.
  - Repro `/tmp/t18_cancel_tb.py` (TestClient, executor returns a CANCELLED verdict — exactly what `_abort_cancelled` produces on a cross-process `nine cancel`): `POST /v1/submit` → `nine.schema_validation.SchemaValidationError: route-event schema violation: 'CANCELLED' is not one of ['SHIP','FIX','BLOCK','UNVERIFIED'] (path: ['verdict'])` → HTTP 500 "Internal Server Error" (with `raise_server_exceptions=False`).
- impact: on the deployed API (JSONL-fallback deployments where `nine cancel` can reach the same ledger file — the documented operator flow T16-F1 made first-class), cancelling a running job turns the submit response into an opaque 500; the operator never gets the `status: cancelled` + CANCELLED verdict payload the CLI path returns. Slice-34 fixed the CLI but not the server that T15-F12/T16-F5 deploy.
- suggested_fix: add the identical early-return to `deploy/server.py:_record_route_event` (`if verdict.get("verdict") == "CANCELLED": return`), and/or register a SchemaValidationError handler that returns a clean 4xx/502 JSON. Regression test: server `_record_route_event` with a CANCELLED verdict must not raise and must write nothing to the events store.
- effort: S

## FINDING 3
- area: CANCELLED verdict durability — `nine/runtime/workflows.py` `_abort_cancelled`
- severity: medium
- title: the CANCELLED verdict is never persisted — T16-F1's "durable CANCELLED verdict" claim only holds in memory; the ledger's terminal row ends with `verdicts: []`
- evidence:
  - `nine/runtime/workflows.py:259-290` — `_abort_cancelled` sets `job.status = "cancelled"`, `job.add_verdict(verdict)` (line 280), then `return` — **no `self.ledger.update(job)`**. The docstring (265-267) rationalizes "the durable ledger already says cancelled … do NOT append a shipped/blocked line over it" — but a cancelled-status row carrying the verdict is not a shipped/blocked stamp; it is the missing audit record.
  - The two abort call sites (`workflows.py:460-462` start-of-attempt and `:659-661` post-nodes) return directly to the caller; neither the CLI `cmd_submit` nor `deploy/server.py:submit` calls `ledger.update` after execute.
  - Repro `/tmp/t18_dur.py` (real WorkflowExecutor + `ledger.cancel` mid-run, same shape as `tests/test_torture_harvest_9.py:429`): executor verdict CANCELLED, in-memory `job.verdicts = ['CANCELLED']`, but durable rows are `submitted → routing → running → cancelled` — the terminal row has `verdicts: []`, `DURABLE CANCELLED verdict present: False`. The slice-34 test asserts only the in-memory `job.verdicts` and the operator's `status == "cancelled"` line, so the "durable" claim in its docstring is unverified-and-false.
- impact: the job record (`nine status`, GET /v1/jobs/{id}) shows a cancelled job with NO verdict, NO summary, NO evidence_refs — the reason/artifacts of the cancel are lost on process exit. The append-only ledger's whole purpose is the audit trail; the terminal verdict is the most important row and it is dropped.
- suggested_fix: after `job.add_verdict(verdict)` in `_abort_cancelled`, call `self.ledger.update(job)` (appends a `cancelled`-status row with the full verdict; status stays cancelled so T8-F3's no-stamp-over-cancel invariant is preserved). Regression test: extend the T16-F1 test to assert the LAST ledger row contains the CANCELLED verdict with evidence_refs/summary.
- effort: S

## FINDING 4
- area: `_git_commit` bool contract / retry path (T16-F9) — `nine/cli.py`
- severity: medium
- title: a RETRY of `nine learn apply`/`revert` after a failed commit silently flips the candidate status with NO commit and NO regression run — T16-F9's "never marked applied on a failed commit" holds only for the first invocation
- evidence:
  - `nine/cli.py:668-671` — `_apply_candidate`: `if kw in current:` → prints "already in catalog … nothing to do" → `learner.cands.update_status(candidate_id, "applied")` → return 0. No regression gate, no `_git_commit`.
  - `nine/cli.py:723-726` — `_revert_candidate`: `if kw not in bucket:` → prints "nothing to revert" → `update_status(candidate_id, "pending")` → return 0. Same.
  - `_git_commit` (786-812) returns False with a loud warning on any non-git deployment (pip/sdist/tarball/Cloud Run image — the docstring's own scenario) or transient failure.
  - Repro `/tmp/t18_learn.py` (commit stubbed False): apply#1 → rc=1, status `pending`, catalog on disk HAS the keyword (T16-F9 correct); apply#2 → "already in catalog — nothing to do", rc=0, **status `applied`** — no commit, no regression, and the warning from run #1 ("candidate was NOT marked applied") is now a lie. Symmetric: revert#1 → rc=1, status `applied`, catalog reverted on disk; revert#2 → rc=0, **status `pending`** — the rollback commit never happens.
- impact: on any non-git install (or after a transient git failure — lock, hook), the durable audit commit never lands yet the candidate flips to applied/pending on the retry; `nine learn` state then disagrees with git history. The exact silent-partial-mutation T16-F9 was built to kill reappears one retry later.
- suggested_fix: in the "already present" branches, only flip the status when the on-disk catalog state is actually committed (e.g. detect the pre-existing mutation — `git status --porcelain` on catalog.json — and commit it before marking applied, or refuse with the same loud warning and leave status untouched). Regression test: `_git_commit`→False twice → status must stay `pending` after both calls.
- effort: S

## FINDING 5
- area: server submit() error surface (T15-F12/T14-F10 contract) — `deploy/server.py` + `nine/runtime/workflows.py`
- severity: medium
- title: server submit() still has raw-500 paths (work-dir-as-file, ChainError) and records durable state BEFORE the learner/memory stores are opened — a bad store 502s an already-committed job and retries duplicate it
- evidence:
  - Raw-500 paths: `deploy/server.py:415-416` (`job_dir.mkdir(parents=True, exist_ok=True)` + `(job_dir/"task.txt").write_text(task+"
")` on the CHAINS path) and `nine/runtime/workflows.py:440` (executor `job_dir.mkdir`) are unwrapped — with `NINE_DATA_DIR/work` a FILE, `mkdir(exist_ok=True)` raises FileExistsError → uncaught → HTTP 500 (repro: `/tmp/t18_server.py` (g), 500 "Internal Server Error"). `deploy/server.py:421` `cex.execute(...)` can raise `ChainError` (chains/chain.py:59,219) which the CLI catches cleanly (`nine/cli.py:198-203`) but the server has NO ChainError handler (only WorkflowError/LedgerUnavailable) → raw 500 (repro (h): 500).
  - Order-of-operations: on the CHAINS path `get_learner()`/`get_memory()` are called at `deploy/server.py:419-420` — AFTER `ledger.submit` (:402) + `ledger.update` (:404); on the WORKFLOWS path `get_learner()` runs inside `_record_route_event` (:458) — AFTER `ex.execute` (:457) completed and SHIPPED. A bad events/memory store (`EVENTS_PATH`/`MEMORY_PATH` blocked, the exact T15-F12 setup) then raises LedgerUnavailable → 502 for a job that is durably committed and consumed quota; the client retries → duplicate job.
- impact: the T15-F12 "clean 502, never raw 500" contract is enforced only on the GET endpoints; the submit path both violates it (FileExistsError/ChainError → 500) and reports failure AFTER success (502 on a shipped job, duplicates on retry). CLI already does both right (OSError wrap at cli.py:184-189, ChainError at 198-203) — the server was not given the same treatment.
- suggested_fix: wrap the mkdir/write_text in OSError → `LedgerUnavailable` (or HTTPException 502), register a ChainError exception handler returning 502, and construct learner/memory (and validate the stores) BEFORE `ledger.submit` so a broken store fails fast with a clean 502 and no job is written. Regression test: work-as-file → POST returns 502 JSON not 500; ChainError → 502; blocked events path → 502 AND `ledger.jsonl` has no rows.
- effort: S

## FINDING 6
- area: redact() credential-key coverage (T16-F4/T15-F6) — `nine/router/classifier.py`
- severity: low-medium
- title: redact() leaks `*_key = <value>` forms — private_key / secret_key / consumer_key / access_key / ssh_key / public_key values pass through untouched
- evidence:
  - All value-redaction patterns (classifier.py:59, 64-68, 70, 73, 83) key on `(password|passwd|pwd|secret|token|api[_-]?key)` — there is NO bare-`key` alternative and no `private_key|secret_key|consumer_key|access_key|client_key|ssh_key` alternative, so `private_key = …` never matches; the final `((?:sk|pk|gh[po])(?![a-z])|AIza)[A-Za-z0-9_\-]{10,}` pattern only catches values whose TOKEN starts with sk-/pk-/ghp-/AIza.
  - Repro `/tmp/t18_a_b.py`: `"private_key = super-secret-value-12345"`, `"client_secret_key = abcdefghijklmnopqrstuvwxyz123456"`, `"consumer_key = 0123456789abcdefghijklmnopqrstuv"`, `"access_key = ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"`, `"ssh_key = abcdefghijklmnopqrstuvwxyz0123456789"`, `"public_key = MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A"` all come back UNCHANGED (fully leaked). Controls still redact (`sk-…`→`sk***`, `pk-live-…`→`pk***`).
- impact: tasks containing PEM/private keys, consumer/access/secret keys (extremely common in deploy/infra prompts — the AKIA path even documents `aws_secret_access_key` separately at :73) are stored and routed with the secret intact; this is precisely the "accidental secret in task text" class the redaction boundary exists for.
- suggested_fix: extend the key alternation with explicit multi-word keys — `(?:private|public|consumer|access|secret|client|ssh)_key` — in the value patterns (NOT bare `key`, which would over-redact prose like "the key is x"); run the full suite to confirm `ski`/`task`/`ask` boundaries still hold. Regression test: the six leak cases above must be redacted; `"the key is on the table"` must stay untouched.
- effort: S

## FINDING 7
- area: evidence-verdict schema audit invariant — `schemas/evidence-verdict.schema.json`
- severity: low-medium
- title: SHIP/FIX/BLOCK verdicts with `gate_version: null` validate — the "which gate certified this" audit invariant is presence-only, not type-enforced
- evidence:
  - Schema: `"gate_version": {"type": ["string","null"]}` with `allOf: [{if: {verdict: CANCELLED}, then: {}, else: {required: [gate_version]}}]` — the else branch demands the KEY's presence only; `null` is an admitted value.
  - Repro `/tmp/t18_a_b.py`: SHIP, FIX, BLOCK AND CANCELLED verdicts with `gate_version: null` all ACCEPTED by `validate("evidence-verdict", …)`. The real gate stamps a non-null string (`nine/gates/evidence.py:24 GATE_VERSION = "0.1.0"`, stamped at :71), so any verdict carrying `null` is by construction NOT gate-produced — yet it validates as SHIP/FIX/BLOCK.
- impact: a verdict that was never certified by a gate version (hand-written ledger line, bug, or replay of the cancelled path) can be recorded as a SHIPPED/FIXED/BLOCKED audit row; the gate-version audit trail ("which gate certified this") cannot distinguish certified from non-certified verdicts at the schema boundary.
- suggested_fix: in the else (non-CANCELLED) branch, also constrain the type — e.g. keep the top-level `["string","null"]` for CANCELLED but add `then`/`else` type assertions (`else: {properties: {gate_version: {type: "string"}}, required: [gate_version]}`), or split into two `oneOf` branches. Regression test: SHIP with `gate_version: null` → SchemaValidationError; SHIP with `"0.1.0"` → valid; CANCELLED with null → valid.
- effort: S

## FINDING 8
- area: mutator boundary strictness (T16-F3) — all five schemas + `Job.add_verdict`
- severity: low
- title: no boundary schema sets `additionalProperties: false` — unknown keys are silently accepted and persisted by every mutator; `add_verdict` has no shipped-job guard
- evidence:
  - All five boundary schemas (`schemas/agent-job.schema.json`, `artifact-manifest.schema.json`, `evidence-verdict.schema.json`, `route-decision.schema.json`, `route-event.schema.json`) have `additionalProperties` unset (verified). Repro: `job.add_artifact({name/path/kind/sha256/size/produced_by/produced_at valid, "typo_key": "x"})` → validated + persisted; same for a verdict/route-event with extra junk.
  - `nine/ledger/ledger.py:128-135` `Job.add_verdict` validates shape only — nothing rejects a verdict appended to a job already `shipped` (status machine allows shipped→archived only, but add_verdict doesn't consult status; today no caller does this, so it is a defensive hole, not an active bug).
- impact: the T16-F3 "boundary objects are validated AT THE MUTATORS — malformed objects rejected, never written" doctrine stops at required-fields; a malformed-but-required-complete object (typo'd key, extra fields) persists silently into the audit trail, and there is no status guard preventing a shipped job from acquiring post-hoc verdicts.
- suggested_fix: add `"additionalProperties": false` to the five boundary schemas (check the suite for legitimate extra fields first — e.g. `eval_results`/`schema` already declared), and in `add_verdict` refuse when `self.status in ("shipped","blocked","failed")` unless the verdict is CANCELLED. Regression test: unknown-key artifact/verdict/route-event → SchemaValidationError; `add_verdict` on a shipped job (non-CANCELLED) → error.
- effort: S

---

### Summary
8 findings (1 high, 4 medium, 1 low-medium, 2 low), every one reproduced hermetically against HEAD `0c92623`, all fixable in <30 lines with a hermetic regression test. The high-value cluster is the CANCELLED/server family (F1-F3 + F5): slice-34's T16-F1 fixed the CLI but left the deployed server with a raw-500 on cancelled jobs, a CANCELLED verdict that never reaches the ledger, and — on the production Firestore backend — a submit path that still writes raw task secrets (F1). F4 shows the T16-F9 "never applied on failed commit" promise breaks on the retry. F7/F8 are schema-boundary hardening gaps in the exact areas slice-34 claimed to close. No style nits. Doc-truth count claims verified (431 passed / 5 skipped); README coverage badge 80% vs 76% measured is cosmetic and not filed.
