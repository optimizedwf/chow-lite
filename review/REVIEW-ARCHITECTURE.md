# chow-lite — Architecture & Code-Quality Audit (TASKMASTER track)

**Reviewer:** senior software architect, judging as a Google SWE (Architecture = 30%)
**Repo:** github.com/optimizedwf/chow-lite (MIT) — `/Users/adam26/chow-work/chow-lite`
**Scope:** every Python file (~3,087 LOC incl. tests/deploy), all 5 JSON schemas, README, SUBMISSION.md, docs/architecture.svg, Dockerfile, deploy scripts, pyproject.toml.

**Method:** read every file; ran the stable test suite (27 passed / 5 skipped offline); verified ADK API usage against the installed `google-adk 2.6.3`; reproduced the P0/P1 findings with executable repros (shell injection, chain-job status, FIX-loop behavior, ADK session reuse, empty-artifact gate).

---

## Executive summary

chow-lite has a genuinely coherent core idea — *exit code ≠ success; nothing ships without evidence* — and the ROUTE → EXECUTE → VERIFY → LEARN loop is honestly implemented for the deterministic path. The state machine, append-only JSONL ledger, artifact manifest with sha256, per-hop gates, and the candidate-only learner are all defensible design. The ADK 2 integration is *API-correct* (verified against google-adk 2.6.3: `InMemoryRunner.run` is sync, `create_session` is a coroutine, `Event.content`/`is_final_response`/`part.function_call` all exist).

But the submission has **two P0s (unauthenticated command injection on the deployed API; a Docker image that cannot boot), several P1s where the code contradicts its own doctrine** (chain jobs never leave `submitted` in the durable ledger; FIX loops don't retry on gate-check failures; `chow submit` ignores the router; learner candidates evaporate; "schema-validated" claims with zero validation code), and a **public repo that does not match the working tree** (20 modified files + untracked tests, despite "repo frozen after submission").

Verdict: strong narrative and demo-ability, but the architecture score will be capped by the gaps between claims and code. The Top-5 fix list at the end is the highest-leverage response to a picky judge.

---

## P0 — critical

### P0-1. Unauthenticated remote command execution via task interpolation in `deploy/server.py`
`deploy/server.py:129-131` builds the workflow command by interpolating the user's task into a `bash -c` string:

```python
cmd = (f"echo '{task[:200]}' > task.txt; "
       f"printf 'Artifact: {decision.workflow_id}\n' > FINAL_REPORT.md; "
       f"printf '{eval_json}' > EVAL.json")
```

`deploy/deploy.sh:20` deploys with `--allow-unauthenticated`, and there is **no authentication on any FastAPI endpoint** (no API key, no header check, no Firestore-auth gating). **Reproduced:** submitting `x'; touch /tmp/chowlite_pwned; echo '` executes the injected command with exit 0 and creates the file. Even innocuous tasks break the shell quoting: `"what's the weather"` makes the command exit 2 and produce no `task.txt`. The same pattern exists in `chowlite/cli.py:115-121` (`cmd_submit`), which is less severe (local CLI) but still wrong.

This is the first thing a security-conscious judge will find: a public, unauthenticated endpoint that runs attacker-controlled shell on a GCP service account.

**Fix:** never shell-interpolate user input. Write the task to `task.txt` with `Path.write_text()` from Python and have the bash node read it (the flagship chains already use this file-interface pattern); or pass the task via `argv` to a non-interpolated script. Add an API-key middleware (`X-API-Key` check against an env var) on `/v1/*`, and use a least-privilege service account.

### P0-2. The Docker image cannot boot — `deploy/` is neither copied nor packaged
`Dockerfile:9-13` copies only `pyproject.toml`, `README.md`, `chowlite/`, `schemas/`; `pyproject.toml:27` packages only `include = ["chowlite*"]`; yet `Dockerfile:19` runs `CMD ["uvicorn", "deploy.server:app", ...]`. `deploy/` is not in the image and not a Python package → `ModuleNotFoundError: No module named 'deploy'` at container start. `gcloud run deploy --source .` (deploy.sh:19) would build this and crash-loop; the README/SUBMISSION "production shape" story falls apart in front of the judge.

**Fix:** `COPY deploy ./deploy` in the Dockerfile (and/or add `deploy` to `packages.find.include`), and add a `python -c "import deploy.server"` smoke check to the build. Add a container boot test to CI.

---

## P1 — should-fix

### P1-1. Chain jobs never leave `submitted` — the durable ledger lies about the flagship feature
`ChainExecutor.execute` (`chowlite/chains/chain.py:81-164`) never transitions the chain `job` through the lifecycle. It only calls `self.ledger.update(job)` (line 162). **Reproduced:** after a successful 3-hop `demo_lane()` run the returned `res["final"] == "SHIPPED"`, but `ledger.get(job_id).status == "submitted"` and `verdicts == []`. The chain-level verdict exists only in the in-memory return value; the durable record — the system's centerpiece — says "submitted" for a finished (or blocked) chain. A judge reading the ledger after the demo will see exactly this.

**Fix:** drive the chain job through `running → awaiting_evidence → shipped | blocked`, append the final chain verdict to `job.verdicts`, and write `completed_at` (reuse `Job.transition`, which already enforces the legal-transition table).

### P1-2. FIX loops don't retry on gate-check failure (contradicts the module doctrine)
`chain.py:15-18` states *"If a hop's gate returns FIX, the hop re-runs (max_fix_loops)."* The loop at `chain.py:99-145` only retries when a **required artifact file is missing** (`missing = [a for a in hop.required_artifacts if not (job_dir / a).exists()]`, lines 136-144). **Reproduced:** a hop whose `EVAL.json` exists but contains a failing check (the most common real failure) returns FIX on attempt 1, `missing == []`, the loop breaks, and the chain BLOCKs immediately — with `max_fix_loops=3` it still ran exactly once. The retry path only handles "no file" failures, so the fix loop is mostly decorative for the flagship's `eval-json` gate.

Related: on the final attempt the hop job is left dangling in `fixing` status (`workflows.py:218-219` sets `fixing` whenever `attempts <= max_fix_loops`; the chain then blocks without ever resolving that hop job). The `fix_directive` written into `chain_inputs` (lines 140-143) is also never consumed by any node — bash nodes receive only `{"task", "node"}` (`workflows.py:144`), so the "rework" directive is inert.

**Fix:** retry on the verdict itself (`verdict == "FIX"`) bounded by `hop.max_fix_loops`; pass `fix_directive` into `node_inputs` and document it; transition the last hop attempt to `blocked` when the chain blocks.

### P1-3. `chow submit` ignores the router — ROUTE is decorative in the primary CLI path
`cmd_submit` (`cli.py:113-128`) builds one hard-coded "collect" bash node for **every** `workflow_id` (research/build/review all produce identical `FINAL_REPORT.md` + `EVAL.json`). **Reproduced:** `chow submit "research the history of the typewriter"` and `chow submit "review the code quality"` produce byte-identical artifacts; only the recorded `workflow_id` differs. The router's decision — the entire ROUTE phase — does not select any behavior. Meanwhile the *real* workflows (flagship chain, demo lane, research workflow) live in a separate registry that `submit` never touches. The workflow catalog is also triplicated: `cli.py:41-51`, `deploy/server.py:52-94`, `demo_live.py:22-38` define different registries (the server's `inbox-triage-task-report` keywords don't exist in `chow submit`, so `chow submit "customer refund…"` falls back to `fallback-respond`).

**Fix:** map `workflow_id → Workflow` in one shared catalog module and have `cmd_submit` dispatch to the registered workflow (or document `submit` as a demo stub and require `chain` for real work). Single source of truth for the catalog.

### P1-4. `ADKAgentNode._session_ready` breaks multi-job reuse (silent empty agent runs)
`adk_runtime.py:51-62`: after the first job, `_session_ready=True` makes `_ensure_session` a no-op for *every later job*, but sessions are keyed per job (`session_id = f"job-{job_id}"`, line 69). google-adk 2.6.3's `Runner.run_async` only auto-creates a missing session when `auto_create_session=True`, and `InMemoryRunner` **does not expose that flag** (default `False`, verified in the installed package). **Reproduced:** running with a never-created session raises `SessionNotFoundError` inside ADK's background thread; the sync `list(runner.run(...))` swallows it and returns **zero events** → `final_text=""` → an empty `agent_output.md` written as "evidence" → the gate FIX/BLOCKs on a phantom. So a node instance reused across two jobs (the normal server/multi-job pattern) fails silently on every job after the first. Also `asyncio.run()` (line 57) raises `RuntimeError` if the node is ever called from an existing event loop.

**Fix:** cache by `session_id`, or create the session unconditionally per job; construct the runner with `auto_create_session=True` (via `Runner` directly); check the event stream is non-empty and surface ADK errors instead of writing empty artifacts; avoid `asyncio.run` (use `run_async` + drain, or a dedicated loop).

### P1-5. The LEARN loop is not durable and not wired to real route decisions
- `Learner.candidates` is an in-memory list (`learner.py:91`); `learn()` scans the event store and re-appends candidates on every call, deduping only by exact description (lines 132-140). Nothing persists candidates, so the documented "candidates queue where a human (or a review hop) approves them" (learner.py:5-7) does not survive a restart. **The only durable artifacts are route events; the learner's *output* is ephemeral.**
- Chain events hardcode `confidence=0.5` and `router_version="chain-v1"` (`chain.py:122-123`), so the learner's own rule "high-confidence route that still FIXed" (`learner.py:125`, `confidence >= 0.7`) **can never trigger from real chain runs** — the actual ROUTE decision is never attached to chain jobs (`demo_live.py` and `ChainExecutor` never call `job.attach_route_decision`), and `task_redacted` is truncated but not `redact()`ed (`chain.py:120`).

**Fix:** persist candidates (JSONL/Firestore) with a real review queue; carry the actual `RouteDecision` (workflow_id, confidence, router version) through `ChainExecutor` into each `RouteEvent`; run `redact()` on the task text; make `learn()` idempotent per event (track processed event ids).

### P1-6. "JSON Schema validation" is claimed everywhere and implemented nowhere
README ("route-decision (schema-validated)", "artifact manifest (JSON schema validated)"), SUBMISSION.md ("JSON Schema validation in tests", "typed schemas for every boundary"), and `classifier.py:93-94` ("Schema validation happens in Router.classify()") — but there is **no validation code in the repo** (no `jsonschema` import or dependency; `test_route_decision_schema_fields` only checks key presence). A judge running `grep -r validate` finds nothing. Additionally the schemas disagree with the code: `schemas/agent-job.schema.json` requires `attempts >= 1` while a fresh `Job` serializes `attempts=0` (`ledger.py:72`); the schema says job `input` is "(redacted)" but `Job.input` stores the raw task (`ledger.py:69`, `server.py:121`, `cli.py:109`).

**Fix:** add `jsonschema` and validate `RouteDecision`, `Job`, `EvidenceVerdict`, `ArtifactManifest`, `RouteEvent` at the boundary (or delete the claim and make the schemas documentation-only, and fix the two schema/code mismatches).

### P1-7. Production durability silently degrades to ephemeral local state
`server.py:40-49` (`get_ledger`): on Cloud Run, if `FirestoreLedger(...)` raises for any reason (credentials, outage), the server silently falls back to `JSONLLedger("jobs/ledger.jsonl")` on an **ephemeral instance disk** — jobs vanish on scale-to-zero and are invisible to other instances. It also constructs a brand-new Firestore client and a brand-new genai client per request (lines 40-94), which is wasteful and can exhaust connections. `firestore.rules` (`request.auth != null`) protects direct client access, but the server SDK bypasses rules and the API has no auth at all (see P0-1) — the "secure API-key auth" line in SUBMISSION.md is not implemented.

**Fix:** fail fast when the configured backend is Firestore; cache ledger/router clients as module-level singletons; add real API-key auth; write Firestore transitions with transactions (see P2-7).

---

## P2 — nice-to-have / polish

- **P2-1 Artifact rollup loses attempt-1 files.** Rollup reads only the *last* attempt's job artifacts (`chain.py:154`); files created by earlier attempts that the final attempt didn't re-touch are absent from the chain job's artifact list. Also, junk intermediates (`_task`, `_src`, `build.log`, seeded `task.txt`) are registered as `document` artifacts (demonstrated: chain artifacts include `_task`). No allowlist/ignore set. Suggest: roll up the union of all attempts' artifacts; ignore files starting with `_` / a node-scoped allowlist.
- **P2-2 `exit_codes_check` is vacuously true with no bash nodes** (`evidence.py:117-118` returns `True, "no bash nodes to verify"`). For LLM-only hops the exit-code check is meaningless; combined with existence-only artifact checks, a prompt node that writes an empty file ships. Consider an explicit `UNVERIFIED`/skip signal that gates must still weigh.
- **P2-3 `required_artifact_check` is existence-only** (`evidence.py:130`) — an **empty** `research.md` passes the flagship "research-md" gate, contradicting `flagship.py:11-12` ("must exist and be non-empty"). Reproduced (empty file → SHIP). Add a `min_size`/non-empty factory.
- **P2-4 `Node.timeout_seconds` is only enforced for bash** (`workflows.py:110-113`); `prompt`/`tool`/`subagent` callables can hang the job forever (the ADK node especially — no timeout on `runner.run`).
- **P2-5 DAG silently ignores `depends_on` on unknown nodes** (`workflows.py:68` `if dep in self.nodes`) — a typo'd dependency is silently dropped; raise at build/validate time.
- **P2-6 Status vocabulary is inconsistent across layers:** gate `SHIP/FIX/BLOCK`, chain `SHIPPED/BLOCKED`, job `shipped/fixing/blocked` — and chain hop jobs dangle in `fixing` after a block (P1-2). Pick one enum.
- **P2-7 Firestore concurrency:** `FirestoreLedger.transition`/`update` are read-modify-write without transactions; two concurrent Cloud Run instances can lose updates on the same job. `discover` streams all docs then sorts in Python (fine at this scale, worth a note + `order_by`).
- **P2-8 `RouteDecision.alternatives` is always `[]`** (`classifier.py:174-183`) — dead field; model confidence is never thresholded and never compared against the keyword fallback score (a confident-but-wrong model route wins over a solid keyword match).
- **P2-9 `redact()` gaps:** no AWS `AKIA…`, `Authorization: Basic`, `ghp_` full-token coverage; and raw tasks are stored unredacted in `Job.input` (P1-6) and in `node_inputs`/artifacts.
- **P2-10 CLI exit-code semantics:** `cmd_submit` returns 0 even when the job is BLOCKED/failed (only the print shows the verdict); `cmd_chain` returns 2 on block — inconsistent. Also `recover()` only flips a status flag; nothing re-executes the job (recovery is a stub).
- **P2-11 `demo_live.py:22-25` raises `KeyError` on `os.environ["GEMINI_API_KEY"]`** when the key is unset, contradicting its own docstring ("Falls back to deterministic routing when no GEMINI_API_KEY is set"). Use `os.environ.get(...)`.
- **P2-12 Claim/code drift in docs:** README says "25 tests"/"22 passing tests" (actual stable suite: 27 passed / 5 skipped; 37 with untracked files); SUBMISSION says "27/27" and "~1,200 lines" (actual ~3,087 incl. tests); README claims "ADK observability for traces" and "ADK evaluate maps to the evidence gate" — no such code exists; `cloud-run.yaml:22` has a literal `gcr.io/PROJECT_ID/chow-lite` placeholder and is **not** used by `deploy.sh` (`--source` buildpacks), so the README's "Deployment: deploy/cloud-run.yaml" is misleading.
- **P2-13 Untested (stable suite):** FirestoreLedger (no test in committed repo — the untracked `test_firestore.py` appeared during this review), ADK multi-job/session reuse (P1-4), the FIX→retry→SHIP happy path (only the BLOCK path is tested), shell-quoting/injection (P0-1), chain-job lifecycle (P1-1), learner candidate persistence, node timeouts, concurrent JSONL appends. `test_server.py` (untracked) even asserts `status_code in (200, 404)` — a test that cannot fail.
- **P2-14 Git hygiene vs. the "frozen repo" claim:** working tree has 20 modified files + 3 untracked test files vs. HEAD; the public repo (54 files, commit `d7acf3a`) is **stale relative to the code under submission**. Commit the working tree or explicitly freeze.
- **P2-15 `make_adk_node` returns an ad-hoc dict** (`adk_runtime.py:114-123`) that callers manually unpack into `Node(...)`; and `kind="subagent"` executes identically to `tool`/`prompt` in `workflows.py:116-120` — "subagent" is a label, not an abstraction. Either make it return a real `Node` or delete it.
- **P2-16 `GeminiRouter` prompt/parse robustness:** JSON extraction relies on `json.loads` of the raw text after a naive fence strip (`classifier.py:115-123`); model output with trailing prose or a JSON-in-fence variant fails to `fallback-respond` — fine for a fallback, but a regex/code-fence extractor is 10 lines.

---

## Top 5 fixes for highest impact

1. **Kill the RCE (P0-1 + P0-2):** never shell-interpolate user task (write files from Python; read `task.txt` in nodes), add API-key auth middleware, and make the Docker image boot (`COPY deploy ./deploy` + package fix). A judge that deploys this gets a crash-loop or a pwned endpoint — everything else is moot.
2. **Make the chain's durable record truthful (P1-1 + P1-2):** transition the chain job to `shipped`/`blocked` with a final verdict, retry FIX on the *verdict* (not missing artifacts), and resolve dangling `fixing` hop jobs. The ledger is the demo's centerpiece; it must not say `submitted` after SHIPPED.
3. **Wire ROUTE → EXECUTE → LEARN for real (P1-3 + P1-5):** single workflow catalog; `chow submit` dispatches by `workflow_id`; `RouteEvent` carries the actual decision/confidence and redacted task; persist learner candidates.
4. **Either implement or retract "schema-validated" (P1-6):** add `jsonschema` validation at the four boundaries (and fix `attempts>=1`, unredacted `input`), or soften all claims in README/SUBMISSION.
5. **Fix the ADK node for multi-job reuse (P1-4):** per-session-id session creation or `auto_create_session=True`, surface empty-event/error cases, and add a non-keyed ADK test (fake model) so the mandatory-ADK story is testable in CI.

---

## What judges will love

- **A real doctrine, enforced in code:** "exit code is not success" is implemented (`evidence.py`), with a legal-transition state machine, append-only JSONL, sha256 artifact manifests, and per-hop gates — not just prose.
- **The ADK 2 integration is API-correct** (verified against google-adk 2.6.3): `LlmAgent` + `FunctionTool` + `InMemoryRunner`, sync `run()`, `create_session` coroutine handling, event parsing via `is_final_response`/`function_call`. The live test (`tests/test_adk.py`) is exactly the kind of proof a rubric wants.
- **Zero-key offline operation:** deterministic router fallback + bash hops mean the entire loop runs in CI and for judges in 5 minutes; 27 tests pass with no credentials.
- **The 5-hop flagship chain + LEARN loop** is a coherent, defensible narrative: departments, evidence handoffs, candidate-only self-improvement (never auto-applies) — good "agent OS" story.
- **Clean repo hygiene at HEAD:** no committed secrets, ledger/work dirs ignored, schemas as first-class artifacts, a real demo script and GCP-proof tooling (`deploy/demo_probe.py`).

## What judges will hate

- **Unauthenticated /v1/submit with shell interpolation** = instant RCE finding (P0-1) — and the Docker image doesn't boot (P0-2), so the live-GCP-proof segment can't be reproduced from the repo.
- **The durable ledger lies:** chain jobs stay `submitted` even when the run shipped (P1-1); hop jobs dangle in `fixing`; FIX loops don't actually retry on the failure mode that matters (P1-2).
- **ROUTE is decorative in `chow submit`** (P1-3): "router-first" but the router's output selects nothing.
- **"Schema-validated" with zero validation code** (P1-6) — a claim a picky judge will test with `grep`.
- **LEARN output evaporates** and is fed hardcoded confidence (P1-5) — the "self-improvement" story is a demo, not a system.
- **Public repo ≠ working tree** with "frozen after submission" in the docs (P2-14).

---

*Audit performed 2026-08-12. All line numbers refer to the working tree at review time; P0/P1 findings were reproduced, not inferred.*
