# TORTURE-TESTER-5 Report — attack surface: workflows + router + CLI + docs

Worker: TORTURE-TESTER-5 (round 4: re-attack after harvest-3 hardening)
Repo HEAD: 346a71a (slice 24) + uncommitted working tree. All repros hermetic
(`.venv/bin/python` / `.venv/bin/nine`, scratch paths under /tmp; repo left
git-clean). No GEMINI_API_KEY used anywhere; no ADK/model nodes invoked.
Baseline: `pytest tests/ -q` = 252 passed, 5 skipped. Already-fixed ledger
rows (T1–T4, S24) were read first and are NOT re-filed; findings below are
fresh instances of *incomplete sweeps* plus new wiring/router/CLI/doc gaps.

## FINDING 1
- area: robustness
- severity: high
- title: T3-F7 write containment was applied to flagship ONLY — 23 model-controlled `write_file` sites in 14 other workflows (incl. new debug/build-multi) still accept `../` escapes; compose can even overwrite real repo source
- evidence: `nine/chains/flagship.py:74` defines `_contained_write` and it is used at exactly 3 sites (flagship.py:47,142,219). Every other ADK FunctionTool writes uncontained: `(job_dir / path).write_text(content)` at `nine/workflows/debug_wf.py:51,134`, `nine/workflows/build_multi_wf.py:53`, and 19 more sites (test_wf.py:53, draft_wf.py:53/139, pipeline_wf.py:76, research_quick_wf.py:90, document_wf.py:72, refactor_wf.py:80/191, transform_wf.py:75, review_multi_wf.py:83/144, compose_wf.py:121/169/230/233, compare_wf.py:93, analyze_wf.py:95, research_deep_wf.py:80/160, extract_wf.py:78). Repro (hermetic):
  ```
  job_dir = Path(tempfile.mkdtemp())
  target = job_dir / "../EVAL.json"          # exactly what debug/build-multi write_file does
  target.write_text('{"checks":[{"name":"pwned","passed":true}],"exit_code":0}')
  # -> file lands OUTSIDE job_dir (work/EVAL.json), silently
  ```
  Worse: `nine/workflows/compose_wf.py:229` writes model-chosen `path` into the REPO with only a `path.endswith("_wf.py")` suffix guard: `"../../workflows/test_wf.py"` passes the guard and resolves to `nine/workflows/test_wf.py` — the model can overwrite real shipped source (verified: `(plugins/"../../workflows/test_wf.py").resolve()` escapes the plugins dir).
- impact: The exact T3-F7 attack (confused/adversarial model writes `../EVAL.json` → poisons another job's evidence, or `../../jobs/ledger.jsonl` / `../../nine/router/catalog.json` → tampering) is fully open on every lane that reached `debug`/`build-multi`/`test`/`transform`/etc. and on the compose meta-lane, which writes into the source tree. The harvest-3 fix gives false assurance ("all 3 flagship sites") while 23 sites share the defect.
- suggested_fix: move `_contained_write` (resolve + `is_relative_to` + refuse) into a shared helper (e.g. `nine/runtime/fsafety.py`) and use it in all 23 sites; for compose, additionally pin the target to the plugins dir and refuse any path whose resolve leaves `_PLUGINS_DIR`. Regression test: parametrized fixture asserting `write_file("../x")` raises for every ADK workflow's closure (import each hop, monkeypatch the agent out, call the FunctionTool with `../EVAL.json`), plus a compose test asserting `"../../workflows/test_wf.py"` is refused.
- effort: M

## FINDING 2
- area: router
- severity: high
- title: Production router selects the CANNED demo chain for real user tasks (`trip`/`refund`/`customer`/`inbox` keywords) and SHIPs boilerplate as a verified job, exit 0
- evidence: `nine/registry.py:231` registers `inbox-triage-task-report: ["trip","plan","refund","customer","inbox"]` in the production keyword catalog; the chain itself (`nine/chains/flagship.py` demo_lane, 3 bash hops) writes canned text (`"Done: routed, executed, evidence-gated."`, flagship.py:503 area) and its EVAL.json is a self-authored pass. Repro (hermetic, no key):
  ```
  .venv/bin/nine --ledger /tmp/l.jsonl --events /tmp/e.jsonl --memory /tmp/m.jsonl submit "customer wants a refund on order 123"
  # route decision: workflow_id=inbox-triage-task-report (reason: customer)
  # chain=inbox-triage-task-report job=... final=SHIPPED   (exit code 0)
  # FINAL_REPORT.md: "# Report / Task: customer wants a refund... / Done: routed, executed, evidence-gated."
  ```
  Also `"book a trip for the family"` and `"help me with my inbox"` route there (verified). The CLI exits 0 on a SHIP; the ledger row says `shipped` with zero model involvement.
- impact: Real user requests are answered by a static demo template and recorded as verified SHIPs — the exact "no stub may SHIP as verified" / "no fabricated output" doctrine (T1-F8/T2-F1) is violated through the ROUTE step, on both CLI and `POST /v1/submit` (server dispatches CHAINS identically, deploy/server.py:283+). The user gets a confident lie with a green exit code and durable evidence record.
- suggested_fix: remove the demo keywords from the production catalog (keep the chain reachable only via explicit `nine chain demo`), or make the demo lane's gate require evidence the canned bash can't produce (e.g. model-written artifact). Regression test: assert router.classify never returns a chain id for non-demo tasks; assert `nine submit "customer wants a refund"` routes to a model-gated workflow.
- effort: S

## FINDING 3
- area: chain
- severity: high
- title: A chain that BLOCKs leaves its container job in `submitted` forever — `discover --status blocked` misses it and `nine recover` refuses it, so a blocked flagship chain is unrecoverable via the CLI
- evidence: `nine/chains/chain.py:231-233` — on `verdict != "SHIP"` the hop loop does `return {"final": "BLOCKED", ...}` BEFORE the end-of-`_execute` `force_terminal(job, "shipped"/"blocked")` (chain.py:250). Container job never leaves `submitted` (only `attach_route_decision` touches it meanwhile). Repro (hermetic, custom bash chain whose hop exits 1; max_fix_loops=1):
  ```
  res = ex.execute(chain, job, {"task": "t"})   # final: BLOCKED
  ledger lines for job: last status = "submitted"
  .venv/bin/nine --ledger ... recover <id>      # error: job <id> is submitted, only blocked/failed can be recovered (exit 1)
  ```
  Verified end-to-end with a seeded ledger. `ledger.recover` (ledger.py:238) only accepts `blocked`/`failed`.
- impact: When any hop exhausts its fix loops (missing artifact, failing EVAL.json after max retries — e.g. a build whose tests keep failing), the operator gets exit 2 from the CLI but the durable ledger says `submitted`; `discover --status blocked` (the documented way to find recoverable jobs) shows nothing; and `nine recover <job>` is REFUSED. The documented recover loop (README "recover a blocked/failed job") is a dead end for the flagship chain.
- suggested_fix: run `force_terminal(job, "blocked")` (and `self.ledger.update(job)`) on the BLOCKED early-return path before returning — or restructure so the terminal-state walk happens in `execute()`'s finally for all outcomes. Regression test: hermetic chain with a failing hop; assert container job's final ledger status is `blocked` and `recover` then works.
- effort: S

## FINDING 4
- area: CLI / robustness
- severity: medium
- title: `nine recover` with a missing `task.txt` silently re-executes the REDACTED task and SHIPs it — recover can destroy the true task text and certify corrupted output
- evidence: `nine/cli.py:362` — after `ledger.recover()` (status transitions to `recovered` BEFORE the wipe), the raw task is read from `task.txt`; if absent, the fallback is `str(job.input.get("task", ""))`, but `job.input["task"]` is the REDACTED text (redaction moved to the ledger boundary, ledger.py:148-152). The comment at cli.py:354 ("the raw task survives in task.txt") is false whenever the workdir was cleaned, moved, or `--workdir` differs. Repro (hermetic): seed a `failed` job whose ledger input is `"customer password=*** please help"` with no workdir; `cmd_recover` → re-executes demo lane → `task.txt` and `FINAL_REPORT.md` contain `"customer password=*** please help"` → `final=SHIPPED`, exit 0. The original raw task is unrecoverable.
- impact: An operator recovering a failed job after any workdir hiccup gets a job that runs and SHIPs on the redacted/corrupted task text — output silently diverges from the true request (and any secrets in the original task are lost from execution entirely). A "recovered SHIP" is then evidence-gated and durable, so the corruption is certified.
- suggested_fix: refuse recover when the raw task is not available: if `task.txt` is missing AND `job.input["task"]` contains a redaction marker (or simply: require task.txt, else error "cannot recover: task.txt missing, raw task not available") before wiping the job dir; also consider storing a raw-task sidecar hash in the ledger so recovery can verify task.txt integrity. Regression test: failed job with redacted ledger input, no task.txt → recover exits non-zero, no re-execution, job stays `recovered`/`failed`.
- effort: S

## FINDING 5
- area: chain
- severity: medium
- title: Standalone `plan` workflow can never SHIP — its gate requires `HANDOFF.md`, which only the research hop's summarize node ever produces
- evidence: `nine/chains/flagship.py:175-183` — `plan_hop()` builds a 1-node workflow (`wf.add_node(_plan_adk_node())`, no summarize node) but its gate requires `required_artifacts=["PLAN.md","HANDOFF.md"]` plus `"handoff-md": required_artifact_check(["HANDOFF.md"])` (flagship.py:181). In the flagship chain, HANDOFF.md comes from the previous research hop; standalone (`nine submit "plan X"` routes to `plan`, registry.py:180 `_wf(plan_hop)`) the job dir is fresh and nothing writes HANDOFF.md. Repro (hermetic — stub the ADK node with a bash node that writes a perfect PLAN.md):
  ```
  verdict: FIX   eval_results: plan-md passed, handoff-md FAILED "missing artifacts: ['HANDOFF.md']"
  job final status: blocked   # even with a perfect plan, forever
  ```
- impact: The `plan` lane is broken as a standalone workflow (permanent BLOCK, burns fix loops + model calls); only the flagship chain path works. Users who route "plan ..." tasks get guaranteed-failed jobs, and the router advertises a lane that cannot ship.
- suggested_fix: either add a distill/summarize step that writes HANDOFF.md in the standalone plan workflow, or make the plan gate require only PLAN.md (HANDOFF.md requirement belongs to the chain's handoff contract, not the plan lane's own gate). Regression test: standalone plan hop with a stub node writing PLAN.md only → assert SHIP; chain path unchanged.
- effort: S

## FINDING 6
- area: robustness
- severity: medium
- title: T4-F5 whitespace-key guard sweep incomplete — 10+ guards still use un-stripped `os.environ.get("GEMINI_API_KEY")` (debug, build-multi, test-side lanes, summarizer), so a whitespace key passes and jobs burn 3 doomed ADK retries
- evidence: `.strip()` was added at only 6 sites (responder.py:44, cli.py:55, test_wf.py:41, flagship.py:35/115/192). Un-stripped guards remain: `nine/workflows/debug_wf.py:39,122`, `nine/workflows/build_multi_wf.py:41`, `nine/workflows/research_quick_wf.py:78`, `nine/workflows/research_deep_wf.py:32`, `nine/workflows/compose_wf.py:32`, `nine/workflows/draft_wf.py:31`, `nine/workflows/transform_wf.py:24`, `nine/workflows/extract_wf.py:66`, `nine/workflows/pipeline_wf.py:28`, and `nine/runtime/summarizer.py:31` (`key = api_key or os.environ.get("GEMINI_API_KEY")` — no strip). With `GEMINI_API_KEY="   "` each guard is truthy → the ADK LlmAgent is constructed with a whitespace api_key → 3 attempts (max_retries=2) of doomed API calls → confusing auth error, not the documented fail-loud "requires GEMINI_API_KEY".
- impact: Same class as T4-F5: wasted retries + misleading diagnostics on every debug/build-multi/transform/... submit when the key is set-but-blank (common shell/.env mistake). The fix is incomplete in exactly the new code this round targets.
- suggested_fix: one shared `env_key()` helper (strip + fail-loud) used by all guards including summarizer; grep audit for `os.environ.get("GEMINI_API_KEY")` without `.strip()`. Regression test: for each workflow hop, monkeypatch `google.adk`/`genai` to raise if constructed, run the node with `GEMINI_API_KEY="   "`, assert WorkflowError mentioning GEMINI_API_KEY and zero client constructions.
- effort: S

## FINDING 7
- area: router
- severity: low
- title: Model router accepts `"confidence": "NaN"`/`"Infinity"` — jsonschema range checks are NaN-blind, so bare `NaN` is persisted in ledger/events as non-standard JSON
- evidence: `nine/router/classifier.py:169` — `conf = float(data.get("confidence", 0.0))`; `float("NaN")` succeeds and `round(nan,3)` is nan. `Router.classify` then runs `validate("route-decision", ...)` (schemas/route-decision.schema.json `"minimum": 0, "maximum": 1`), but jsonschema's `instance < minimum` comparison is False for NaN, so validation PASSES (verified: no exception, `decision.confidence == nan`). The CLI prints `json.dumps(decision.to_dict())` → `NaN`, and `ledger.update` → `_append` (`nine/ledger/ledger.py:173` `json.dumps(job.to_dict())`) writes `"confidence": NaN` — verified with a strict parser: `json.loads(line, parse_constant=...)` REJECTS the ledger line. Route events get the same NaN.
- impact: A single malformed model response (or a model emitting `NaN`/`Infinity`, both cheap to trigger) poisons the durable ledger/event JSONL with RFC-8259-invalid `NaN` tokens — external consumers (jq, JS, Firestore, strict parsers) fail on the whole file; the router's own "schema-validated at every boundary" claim (README) is false for this case.
- suggested_fix: in `Router.classify`, reject/clamp non-finite confidence (`math.isfinite(conf) and 0 <= conf <= 1` else treat as unparsable → keyword fallback), and add a finite check in the schema validator (custom `finite` keyword or a pre-validate `math.isfinite` scan). Regression test: FakeModel returning `"confidence": "NaN"` and `"Infinity"` → Router falls back to keywords, ledger lines remain strict-JSON parseable.
- effort: S

## FINDING 8
- area: docs
- severity: low
- title: README/roadmap claims that are false of the code: test counts, `research.md + EVAL.json` artifact claim, exit-code table, and `debug`/`test` DAG descriptions
- evidence:
  - README.md:228 "tests/ 25 tests" and README roadmap "22 passing tests" — actual suite: 252 passed, 5 skipped.
  - README.md:165 (quickstart) "# → workflow runs, produces research.md + EVAL.json" — the `research` workflow (registry.py:180 → research_hop) produces research.md + HANDOFF.md only; no node writes EVAL.json (verified by node list: research ADK node + summarize node). Its gate certifies artifacts, not EVAL.json.
  - nine/cli.py:18 "Exit codes: 0 ok, 1 error" — `cmd_submit`/`cmd_chain` return 2 for non-SHIP/BLOCK (cli.py:149, cli.py:213); README never documents exit 2 for automation.
  - docs/WORKFLOW-ROADMAP.md `debug` row claims a "symptom (prompt model)" node and lists ROOT_CAUSE.md as evidence; the implemented DAG (debug_wf.py) has diagnose+patch+verify only and the code comment (debug_wf.py:257-260) explicitly says ROOT_CAUSE.md is advisory, NOT gate evidence. The `test` row claims a "spec-reader (tool/ADK)" node; test_wf.py has only test-writer + test-runner.
- impact: Operators/automation trust the README exit-code contract (missing 2), the research-lane artifact contract (expect EVAL.json that never appears), and stale test counts; roadmap misleads contributors about the debug/test DAGs (gate evidence vs advisory docs).
- suggested_fix: sync README counts + exit-code table (0 ok / 1 error / 2 non-SHIP), change the quickstart example to "research.md + HANDOFF.md", and correct the roadmap debug/test rows to the implemented DAGs; add a doc-truth CI check that greps README for test counts and diffs them against `pytest --collect-only`. Regression test: a lightweight script asserting README's "N tests" equals collected count and that claimed artifacts appear in each hop's `required_artifacts`.
- effort: S

---
Summary: 8 findings — 3 high (uncontained write_file sweep incomplete incl. compose repo-write; router SHIPs the canned demo lane; blocked-chain container stuck at `submitted` and unrecoverable), 3 medium (recover re-executes redacted task when task.txt missing; standalone `plan` can never SHIP; whitespace-key guard sweep incomplete), 2 low (NaN confidence poisons ledger JSON; README/roadmap doc-lies). All repros hermetic; repo left git-clean.
