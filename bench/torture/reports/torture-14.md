# TORTURE-TESTER-14 Report — round 7: LEARN loop/catalog, memory graph, deploy server API, demo_live, gate/convert_to_pytest deep-dive, EVAL strict-boolean edges, router keyword substrate, NEW fixture specs 012+

Worker: TORTURE-TESTER-14 (round 7). Repo HEAD: 54a0c83 (slice 32).
All repros hermetic (no Gemini, no network, no quota): `.venv/bin/python` scripts under
/tmp/torture14/ (g1..g9 files), stub/monkeypatch only. No repo files modified. Hygiene note:
one early server repro (before I pointed it at a temp NINE_DATA_DIR) wrote to the repo's
UNTRACKED `jobs/` stores; all MY writes were removed and verified gone (events 2035, ledger
14514 lines after cleanup; git status shows only the pre-existing `bench/state.json`
modification). Concurrent agent processes on this machine also write `jobs/` during the
session — an observed burst at 04:08:14-25 added 15 events/39 ledger lines/3 demo-chain
memory entries from a FOREIGN process (respond/review workflow runs + inbox-triage memory —
not mine, left in place). All later repros set NINE_DATA_DIR/NINE_PLUGIN_REGISTRY to temp
paths so nothing else touched the repo. Full repro scripts kept in /tmp/torture14/ for triage.

Surfaces that HOLD after re-attack (not re-filed):
- EVAL strict-boolean contract (S24-F1) — 15-shape battery at the gate: only literal JSON
  `true` SHIPs; `"true"`/`"false"`/`1`/`0`/`null`/missing/`"TRUE"`/`1.0`/mixed/empty/root-not-object/
  invalid-JSON all FIX (repro `/tmp/torture14/g9_eval_battery.py`).
- demo_live fail-loud: no key on either backend → `no LLM key for active backend (demo_live)`,
  SystemExit, before any route/execute (both backends probed).
- Chain stale guard / fix-directive bleed / durable-ledger recover / hop-id recover refusal /
  dead-keyword drop / plugin-warning / ledger-OSError CLI fixes (T12-F1..F8) — re-read the
  code paths, no regression found on the surfaces I touched.
- Router word-boundary substrate, NaN/Infinity confidence guards, redact() patterns.

Findings below are NEW: the T5-F2 demo-lane routing ban is re-openable through the catalog
(the LEARN write target), the memory write-path stores RAW unredacted handoffs (never
"distilled"), the T11-F3 "N failed, 0 passed" lie survives in two failure branches, the
recursive manifest certifies pytest cache/`.pyc` files, the server returns `verdict: {}` for
shipped chains, convert_to_pytest emits tests that NameError at run time on runner-local
names, and the T12-F8 clean-error contract is still not applied to the learn/memory stores,
`learn apply`, and the server surface.

---

## FINDING 1
- area: router keyword substrate + catalog (LEARN write target) / chains
- severity: high
- title: The T5-F2 demo-lane routing ban is re-openable through catalog keyword overrides — `_merged_keywords()` keeps keywords for CHAIN ids (they are "executable"), so a catalog entry (the documented `nine learn apply`/manual edit surface) for `inbox-triage-task-report` routes real production submits into the CANNED demo chain, which SHIPs boilerplate as verified
- evidence: `nine/registry.py:330-334` — the T12-F6 dead-id filter drops ids not in `WORKFLOWS|CHAINS`; `inbox-triage-task-report` IS in CHAINS, so a catalog keyword for it survives the merge with NO warning. `build_default_router()` (`nine/cli.py`) and `build_router()` (`deploy/server.py`) register every `KEYWORDS` id verbatim; `cmd_submit`/POST dispatch any id in CHAINS (`cli.py` `_execute_job`, `server.py submit`). Repro (`/tmp/torture14/g1_chain_reroute.py` + hermetic server `/tmp/torture14/g4_server2.py`): catalog `{"keyword_overrides": {"inbox-triage-task-report": ["refund"]}}` → `_merged_keywords()` keeps it → `Router.classify("customer wants a refund on their order")` → `workflow_id='inbox-triage-task-report'`; POST /v1/submit → **200, `status: shipped`, `final: SHIPPED`** — the demo lane ran and shipped (artifacts: `_task`, `triage.md`, `EVAL.json`, `task_result.md`, `FINAL_REPORT.md`). T5-F2's fix only removed the demo keywords from `_BASE_KEYWORDS`; the catalog merge path (the ONLY file the LEARN loop may write) re-adds them.
- impact: any human/learn-approved catalog edit (the apply-refusal message even instructs "edit nine/router/catalog.json manually") can silently re-expose the canned demo chain to production traffic — real user tasks ("customer wants a refund") SHIP hardcoded boilerplate as verified jobs with exit 0, the exact T5-F2 integrity failure. Combined with Finding 7's store-construction crash the failure mode is invisible until a canned report ships.
- suggested_fix: refuse catalog keywords for demo-only chain ids (flag `inbox-triage-task-report` as non-routable; drop + loud warning in `_merged_keywords` like the dead-id filter, and refuse in `_apply_candidate`). Regression test: catalog override for the demo chain id is dropped with a warning and `Router.classify` can never emit it; a server submit of a demo-keyword task routes to `respond`/`plan` instead.
- effort: S

## FINDING 2
- area: memory graph (write-path) / chains
- severity: high
- title: The memory write-path stores RAW artifact content — the plan hop's HANDOFF.md (up to 2000 chars, unredacted) becomes the "summary" for EVERY artifact of EVERY later hop: credentials the model echoes from the raw task land verbatim in memory.jsonl (Firestore on Cloud Run), contradicting the module contract "distill then store (never raw)"
- evidence: `nine/chains/chain.py:309-341` `_save_memory` reads `job_dir/HANDOFF.md` verbatim (`handoff.read_text(...)[:2000]`, chain.py:324) and stamps it as `summary` on every `save_artifact_summary` call; `nine/memory/graph.py:4` documents the write-path as "distill then store (never raw)". Repro (full flagship chain, fake models, `/tmp/torture14/g2c_debug.py`): plan model writes HANDOFF.md containing `aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY` + `API_KEY=sk-...`; after SHIP, memory.jsonl has **18 records, ALL with the raw handoff as summary** (including the secrets) — plan/build/review/teach artifacts all "summarized" as the plan handoff; even `.pytest_cache`/`.pyc` records carry it. `task_redacted` is redacted; `summary` is not — a second leak surface past T4-F4 (which only covered the ledger boundary).
- impact: the semantic memory store (searchable via `nine memory search`, persisted to Firestore on Cloud Run) accumulates raw model output including credentials; the "distilled summary" promise is false; build/review/teach memory entries misattribute the plan handoff as their own content, so `search_context` returns misleading context. Same leak class as T2-F6/T4-F4 (rated high when fixed at the ledger boundary), now on a new store.
- suggested_fix: run the stored summary through `redact()` AND a real distill step (summarizer when available; otherwise a capped/whitelisted excerpt), and attach per-artifact context (use the artifact's own content head, not the plan handoff, for non-plan hops). Regression test: chain run where HANDOFF.md contains `aws_secret_access_key = ...`/`API_KEY=` → memory records contain no credential patterns, and build-hop memory entries do not quote the plan handoff.
- effort: S

## FINDING 3
- area: gate/verify message layer (flagship build self-test + test lane runner)
- severity: medium
- title: T11-F3 fix is INCOMPLETE — the failure branches of the flagship build self-test and the `test` lane runner still count `' PASSED'` with `grep -c` against pytest `-q` output, so a failing run reports "N test(s) failed, 0 passed" even when tests passed (the exact lie T11-F3 was filed for)
- evidence: `nine/chains/flagship.py:305` and `nine/workflows/test_wf.py:129` — `passed=$(grep -c ' PASSED' test_output.log ...)` (pytest `-q` never prints ` PASSED`; only the summary line "N passed" exists). `debug_wf._build_verify_command` was fixed to `grep -oE '[0-9]+ passed'` but these two sites were not. Repro (`/tmp/torture14/g5_flagship_count.py`, runs the REAL `_build_self_test_command` bash on a 5-test file with 3 failing): EVAL.json message = **"3 test(s) failed, 0 passed"** for an actual 3-failed/2-passed run.
- impact: the build/test agent's fix directive claims zero passing tests when 2 passed — a model may rewrite passing tests, waste budget, and the EVAL evidence record misreports the run. The strict boolean verdict is still honest (passed:false), so this is a message/directive lie, not a SHIP lie — same severity class as T11-F3 (med).
- suggested_fix: in both failure branches parse the summary line like debug_wf (`grep -oE '[0-9]+ passed' test_output.log | tail -1`), fall back to 0. Regression test: run the command strings against a synthetic 3-failed/2-passed log and assert the EVAL.json message says "2 passed".
- effort: S

## FINDING 4
- area: artifact manifest + memory graph (recursive sweep)
- severity: medium
- title: The T11-F5 recursive manifest has NO ignore filter — pytest runtime cache (`.pytest_cache/*`, `__pycache__/*.pyc`) and `test_output.log` are certified as shipped evidence and memorized; the build hop can also ship TWO conflicting EVAL.json entries (build node + self-test node both write EVAL.json)
- evidence: `nine/runtime/workflows.py:116-129` `_manifest_files` registers every non-symlink file under the job dir with no exclusion list. Repro (`/tmp/torture14/g2c_debug.py`): shipped chain manifest contains `.pytest_cache/.gitignore`, `.pytest_cache/CACHEDIR.TAG`, `.pytest_cache/README.md`, `.pytest_cache/v/cache/nodeids`, `__pycache__/solution.cpython-314.pyc`, `__pycache__/test_solution.cpython-314-pytest-9.1.0.pyc`, `test_output.log`, AND two `EVAL.json` entries (69B "by build", 91B "by self-test" — both written this attempt, so the T10-F2 stale guard cannot see the conflict; T12-F4 only fixed the REVIEW hop's clobber). The memory graph then stores "summaries" for the cache files too (see Finding 2).
- impact: the shipped manifest certifies binary pyc bytes and pytest cache internals as job evidence — any consumer (artifact listing, replay, evidence QA, memory search) treats runtime cache as produced artifacts; two sha256s for one logical EVAL.json name make the manifest self-contradictory in the build hop; `.pytest_cache`/`.pyc` noise swamps real artifacts in `nine artifacts` and memory search.
- suggested_fix: exclusion set in `_manifest_files` (`.pytest_cache`, `__pycache__`, `*.pyc`, `test_output.log` or any `*.log`), and per-attempt name-dedupe that keeps the gate-relevant producer (self-test) when two nodes write the same filename. Regression tests: build-hop manifest has exactly one EVAL.json matching the self-test sha; no `.pytest_cache`/`.pyc` entries in a SHIPPED chain manifest.
- effort: S

## FINDING 5
- area: bench gate machinery (convert_to_pytest deep-dive)
- severity: medium
- title: `convert_to_pytest` emits test functions that reference runner-local constants/helpers which are never imported — the converted suite fails at RUN time with NameError ("2 failed"), so the debug fix-loop instructs the model to fix a bug that lives in the SEEDED test file the model cannot edit → guaranteed BLOCK after burning budget; T11-F7's warning only covers nested calls, not dangling names
- evidence: `bench/bench_nine.py:118-160` — the converter copies only `from solution import <imported names>` and the `test(...)` calls; any name used in an expression that is not imported from `solution` is left dangling. Repro (`/tmp/torture14/g3_convert2.py`): runner with `EXPECTED_SUM = 5` and a helper `twice()` referenced by top-level `test("...", lambda: add(2,3), EXPECTED_SUM)` → converted pytest runs as **`test_01_adds_with_constant - NameError: name 'EXPECTED_SUM'`, `test_02_helper_in_body - NameError: name 'twice'` (2 failed, 1 passed)** — run-time errors, not collection errors, so the verify node reports plain failures and the fix directive blames the solution. Current fixtures 001-009 are written in a single-expression lambda style that happens to dodge this, but any natural runner using constants/helpers silently produces an unfixable fixture (future fixture authors, including specs 012+ below, will hit it).
- impact: a legitimate fixture style produces a permanent FIX→BLOCK loop with misleading directives; the bench's `[warn] convert failed` path does not trigger because conversion "succeeds".
- suggested_fix: after unparsing, walk the converted body for `ast.Name` loads that are neither builtins/imported-names nor params, and either inline the value (when the name resolves to a module-level constant in the runner via `ast.literal_eval`) or fail conversion loudly with the dangling names listed. Regression test: constant/helper runner → conversion refuses (or inlines) with a loud message, never a silent NameError suite.
- effort: M

## FINDING 6
- area: deploy server API (chain submit response contract)
- severity: medium
- title: POST /v1/submit for any CHAIN route returns `"verdict": {}` with `status: shipped` — `ChainExecutor.execute` returns no `verdict` key, so the API claims verified success while the container job carries `verdicts: []` and zero evidence in the response
- evidence: `deploy/server.py:352` — `"verdict": res.get("verdict", {})`; `ChainExecutor.execute` returns `{"final", "hop_results"}` (chain.py) with no verdict key. Repro (`/tmp/torture14/g4_server2.py`): POST → `{job_id, status: shipped, final: SHIPPED, verdict: {}, decision}`; `GET /v1/jobs/{id}` → `verdicts: []` on the container job (per-hop verdicts live on hop jobs the API never links). `demo_probe.py` prints `b.get('verdict', {}).get('verdict')` → `None` on a shipped job.
- impact: any API consumer (demo probe, dashboards, automation) sees a shipped job with no evidence — the "exit code is not success; evidence gates produce verdicts" contract is invisible on the chain path; a BLOCKED chain still returns HTTP 200 with `status: blocked` and `verdict: {}`, indistinguishable from success without reading status.
- suggested_fix: surface the per-hop evidence: `"verdict": {"verdict": "SHIPPED"/"BLOCKED", "hops": res.get("hop_results", {})}` (or attach the per-hop verdict records). Regression test: chain POST body has a non-empty verdict/evidence field and the container job record exposes hop verdict refs.
- effort: S

## FINDING 7
- area: memory graph + LEARN store construction (CLI)
- severity: low
- title: Store-construction OSError still raw-tracebacks on the learn + memory surfaces — `nine learn events|scan` with a bad `--events` path (FileExistsError from `RouteEventStore.__init__` mkdir) and `nine memory list|search`/`nine chain` with a bad `--memory` path crash with full tracebacks; T12-F8 fixed the ledger only
- evidence: `nine/learn/learner.py:71,111` and `nine/memory/graph.py:59` — `self.path.parent.mkdir(parents=True, exist_ok=True)` is outside any OSError→clean-error wrap. Repros: `nine --events <file-as-parent>/events.jsonl learn events` and `nine learn scan` → `FileExistsError: [Errno 17] File exists` traceback (verified); `nine --memory <file-as-parent>/memory.jsonl memory list` → same FileExistsError traceback (verified). `cmd_memory` only catches OSError around the READ, not construction.
- impact: the T2-F7/T4-F2/T6-F8/T12-F8 clean one-line error contract is not applied to the learn/memory stores; a typo'd path on any LEARN or memory command produces a raw Python traceback (exit 1 with noise), and `nine chain --memory <bad>` dies before executing.
- suggested_fix: wrap mkdir/touch in `RouteEventStore.__init__`, `CandidateStore.__init__`, `LocalMemoryGraph.__init__` and convert to the one-line `error:` contract (mirror T12-F8). Regression tests: learn events/scan + memory list/search/chain with an unusable path print one clean line, no traceback.
- effort: S

## FINDING 8
- area: LEARN loop / catalog apply-revert
- severity: low
- title: `nine learn apply`/`revert` raw-traceback (AttributeError 'str' object has no attribute 'append'/'remove') on a valid-JSON wrong-shape catalog entry (`keyword_overrides[wf]` is a string) — T6-F6 guards `_merged_keywords` at import, but the apply/revert mutation paths never re-validate the bucket
- evidence: `nine/cli.py:549-601` `_apply_candidate` — `current = catalog.setdefault("keyword_overrides", {}).setdefault(wf_id, [])` then `current.append(kw)` (cli.py:586); `_revert_candidate` `bucket.remove(kw)` (cli.py:626). Repro (`/tmp/torture14/g6_learn_apply.py`, with `_regression_green`/`_git_commit` stubbed): catalog `{"keyword_overrides": {"research": "oops-not-a-list"}}` + a low-confidence research event → `nine learn apply <cand>` → **AttributeError: 'str' object has no attribute 'append'**.
- impact: the same wrong-shape catalog that T6-F6 degrades at import still crashes the operator's apply/revert workflow with a raw traceback instead of the established shape-guard warning; a corrupt-but-valid catalog entry blocks the LEARN loop's human-apply path entirely.
- suggested_fix: type-check the bucket in `_apply_candidate`/`_revert_candidate` (non-list → loud warning + rc 1, never mutate). Regression test: string-valued `keyword_overrides[wf]` → apply returns rc 1 with the shape warning, catalog untouched.
- effort: S

## FINDING 9
- area: memory graph CLI read path
- severity: low
- title: A valid-JSON wrong-shape memory line crashes BOTH `nine memory list` and `nine memory search` with KeyError — T6-F3's ledger shape-guard (`_looks_like_job`) was never applied to memory records; T6-F8 only covered corrupt/non-UTF8 lines
- evidence: `nine/cli.py:88` (search) indexes `h['memory_id']`, `h['hop_id']`, `h['artifact_name']`, `h['verdict']`, `h['created_at']`, `h['summary']`; `cli.py:119` (list) indexes `h['chain_id']`, `h['hop_id']`. Repro (`/tmp/torture14/g7_mem_shape.py`): a line `{"memory_id": "mem-x", "unexpected": 1}` → `nine memory list` → **KeyError: 'chain_id'**; a line `{"summary": "hello secret world", "artifact_name": "a"}` that MATCHES a search query → `nine memory search hello` → **KeyError: 'memory_id'**.
- impact: one hand-edited or version-skewed record (the store is a plain JSONL any editor can touch) bricks the memory CLI with a raw traceback, the same class T6-F3 eliminated for the ledger.
- suggested_fix: shape-guard memory records on read (skip non-dicts and records missing required keys, count them like the ledger's corrupt_lines). Regression test: list + search over a store containing a wrong-shape record return the healthy records with no traceback.
- effort: S

## FINDING 10
- area: deploy server API (error contract)
- severity: low
- title: An unusable NINE_DATA_DIR turns every /v1 endpoint into a raw 500 "Internal Server Error" — the LedgerError raised inside `_LazyFallbackLedger`'s JSONL fallback construction escapes (no handler), and `_ledger_failed` never latches, so every request re-attempts Firestore and re-warns
- evidence: `deploy/server.py:155-186` `get_ledger()` — the query-failure wrapper (`server.py:196-218`) sets `self._fallback = JSONLLedger(LEDGER_PATH)` inside the except; with a bad data dir that construction raises LedgerError, the wrapper never assigns, `_ledger_failed` stays False, and each request repeats the Firestore attempt + warning. Repro (`/tmp/torture14/g8b_server_misconfig.py`, NINE_DATA_DIR = a file): GET /v1/jobs, /v1/stats, /v1/events → all **500 "Internal Server Error"** with the warning re-printed per request; /health still 200 (misleading liveness).
- impact: misconfig (or a full /tmp on Cloud Run) silently breaks the whole API with opaque 500s; the T12-F8 clean-error contract (one line, reason visible) is not applied to the server surface, and the repeated Firestore attempts + fallback warnings spam the logs.
- suggested_fix: wrap `JSONLLedger(...)` construction in get_ledger with except LedgerError → JSONResponse({"detail": str(e)}, 502) (mirror the WorkflowError handler), and latch `_ledger_failed` on fallback-construction failure so the retry storm stops. Regression test: bad NINE_DATA_DIR → /v1/* returns one clean JSON error with the reason, /health honest.
- effort: S

---

## NEW FIXTURE SPECS 012+ (proposals — read-only, not built)

Four specs mapping 1:1 to already-fixed nine invariants (each is a "make the model internalize the doctrine" bugfix fixture; format matches 001-009: task.md + expected-behavior.md + rubric.json + starter with real bugs + tests/check.sh with discriminating cases). 010 (cooperative cancellation) and 011 (atomic JSONL append) remain deferred; these are NEW.

### bugfix-small-012 — stale-evidence honesty (per-attempt provenance)
- maps to: T10-F2 / T11-F5 / T12-F1 (stale-artifact guard; a retry that produces nothing must never SHIP on the previous attempt's artifacts).
- task.md: implement `run_with_evidence(fn, *, attempts=3)` — each attempt calls `fn()` which returns `(evidence, ok)` or raises; on `ok` return the evidence; a failed attempt MUST NOT be followed by returning the PREVIOUS attempt's evidence — after the last attempt raise `EvidenceStaleError` (never silently reuse attempt-1 output; a retry whose producer wrote nothing must fail loud, never certify old work).
- starter bugs (4): (1) caches attempt-1 evidence and returns it when attempt 2 fails (stale SHIP); (2) returns cached evidence even when the producer raised before writing anything; (3) accepts `attempts=0` (no validation); (4) swallows the last exception and returns `None` on final failure.
- rubric dims: fresh-evidence-only (0.3), no-silent-reuse (0.25), failure-honesty (0.25), validation (0.1), style (0.1).
- check.sh cases (12): success-first; success-after-retry; retry-that-raises-then-succeeds; failed-final → raises EvidenceStaleError AND the caller can prove attempt-1 evidence was never returned; producer-raises-empty on attempt 2 → raise, no reuse; attempts=0/-1 → ValueError; attempts=1 → single call; non-callable fn → TypeError; evidence identity check (attempt-2 evidence object, not attempt-1); counter proves exactly N calls; stale cache never escapes even when attempt 2 returns ok=False; stdout/stderr cleanliness.
- discriminates: correct 12/12, starter 5/12.

### bugfix-small-013 — strict-boolean EVAL parsing
- maps to: S24-F1 (only literal JSON `true` passes; `"false"`/1/0/null never pass).
- task.md: implement `eval_passes(checks: list[dict]) -> tuple[bool, list[str]]` — a check passes ONLY when `passed is True` (literal boolean); strings ("true"/"false"), ints (1/0), None, missing → fail with the check name; non-list `checks` and non-dict entries → raise `EvalFormatError`; an empty list → `(False, ["no checks"])`.
- starter bugs (4): (1) `bool(c.get("passed"))` truthiness — `"false"` and `1` pass; (2) `c.get("passed") == "true"` string comparison — `"TRUE"`/`1` mishandled; (3) treats missing `passed` as pass; (4) returns `(True, [])` on empty checks (nothing verified).
- check.sh cases (12): literal true passes; string "true"/"false"/"TRUE" fail; int 1/0 fail; float 1.0/0.0 fail; None fail; missing key fail; mixed list → only the true one passes and failed names listed; empty list → false; checks=None / dict → EvalFormatError; non-dict entry → EvalFormatError; all-true list → (True, []); failed-names order preserved.
- discriminates: correct 12/12, starter 4/12.

### bugfix-small-014 — secret redaction at the log boundary
- maps to: T2-F6 / T4-F4 / T6-F4 (redact at every durable boundary; case-insensitive; quoted/JSON forms; comparison tails; AWS/Slack/Bearer patterns).
- task.md: implement `redact(text: str) -> str` masking credential-shaped substrings: `KEY=VALUE` and `KEY: VALUE` for password/passwd/pwd/secret/token/api_key (case-insensitive), `==`/`!=`/`~=` comparison tails, JSON-quoted `"api_key":"sk-..."`, `aws_secret_access_key = ...`, `AKIA[0-9A-Z]{16}`, `xoxb-...`, `Bearer <token>`, `-----BEGIN ... PRIVATE KEY-----` blocks, and bare `sk-`/`pk-`/`ghp_`/`AIza` tokens ≥10 chars. Everything else must pass through UNCHANGED (no over-redaction: "tokenize the sentence" stays intact).
- starter bugs (4): (1) case-sensitive (API_KEY leaks); (2) misses `==`/`!=` tails (leaks the value); (3) misses JSON-quoted forms; (4) over-redacts innocuous text (e.g. any 4-letter word after "token") or misses AWS/Slack patterns.
- check.sh cases (12): `API_KEY=sk-...`; `password: hunter2`; `password == hunter2`; `"api_key":"sk-123"`; `aws_secret_access_key = AKIA...`; `AKIA0123456789ABCDEF`; `xoxb-1234567890-abc`; `Bearer eyJhbGciOi...`; PEM block; `tokenize the sentence` unchanged; `my email is a@b.com` unchanged; empty input.
- discriminates: correct 12/12, starter 5/12.

### bugfix-small-015 — process-group timeout cleanup
- maps to: T8-F5 / T11-B1 (timeout must kill the whole process group; no orphaned grandchildren dropping ghost files).
- task.md: implement `run_with_timeout(cmd: list[str], timeout: float) -> subprocess.CompletedProcess` — starts the child in its OWN process group (start_new_session), on timeout SIGTERMs the GROUP, waits `grace=0.5s`, SIGKILLs the group if needed, and must guarantee the grandchild (a `sleep 60` spawned by the child) is dead when it returns (checked by a fresh `pgrep`); raise `TimeoutExpired` after cleanup; never leave an orphan.
- starter bugs (4): (1) kills only the shell PID (orphaned grandchild); (2) no start_new_session — SIGTERM can hit the caller's group; (3) no grace/SIGKILL escalation — a SIGTERM-ignoring child survives; (4) returns normally (exit 0) instead of raising on timeout (silent success).
- check.sh cases (12): child+grandchild both dead after timeout (pgrep assert); quick command returns result before timeout; timeout with SIGTERM-ignoring child → SIGKILL escalation; raises TimeoutExpired exactly once; returncode captured for fast path; no process-group escape (caller's shell unaffected); grace < timeout; stdout captured; timeout=0 → immediate; negative timeout → ValueError; no zombie processes after cleanup (ps -o stat); idempotent kill (already-dead child).
- discriminates: correct 12/12, starter 4/12.

---

## Round summary
- New findings: 10 (2 high, 4 medium, 4 low) — every one has a hermetic repro under /tmp/torture14/.
- Surfaces that HOLD: EVAL strict-boolean battery (15 shapes), demo_live fail-loud on both backends, T12-F1..F8 code paths re-read, router word-boundary substrate, redact() patterns.
- Highest-value fixes for slice 33: Finding 1 (catalog re-enables the canned demo lane — T5-F2 regression, high, effort S), Finding 2 (memory stores raw secrets — high, effort S), Finding 3 (T11-F3 incomplete, S), Finding 4 (manifest cache pollution, S). Fixtures 012-015 are build-ready specs.
