# TORTURE-20 — Workflow/Registry/Gate wiring + docs truth (read-only sweep)

**Repo**: chow-lite @ 00cbc46 ("slice 35") · **HEAD**: 00cbc469 (only `bench/torture/LEDGER.md` modified locally)
**Scope**: nine/registry.py, nine/cli.py, deploy/server.py, nine/gates/evidence.py, nine/runtime/workflows.py, nine/runtime/responder.py, nine/workflows/analyze_wf.py, nine/router/classifier.py, README.md, SUBMISSION.md, .env.example, nine/chains/plugins/.
**Method**: read-only; all repros hermetic via `.venv/bin/python` probes (stubbed model call where noted, scratch ledgers in /tmp, no keys used/printed, no repo file modified except this report + LEDGER rows).

**Test baseline (re-verified)**: `pytest --collect-only -q` → **452 collected**; full suite → **447 passed, 5 skipped**.

6 findings: 2 high, 2 medium, 2 low.

---

## FINDING 1
- area: workflow DAGs / gate wiring (`nine submit`, `nine recover`)
- severity: high
- title: 11 router-selectable workflow ids are missing from `_HOPS` — 7 of them can NEVER SHIP via the CLI (generic gate demands EVAL.json their DAGs never write) and every run burns the FIX-loop quota before BLOCKing
- evidence:
  - `nine/registry.py:126` `_HOPS` contains only 13 ids (research, plan, build, review, review-multi, test, build-multi, debug, deploy-check, document, research-quick, refactor, teach) + respond.
  - Missing but router-reachable (keywords in `_BASE_KEYWORDS`, `nine/registry.py:244`+): `transform, pipeline, analyze, compare, compose, draft, draft-email, extract, ideate, research-deep, summarize-standalone` (verified: keyword router emits every one of these ids — e.g. "analyze the sales dataset" → analyze, "summarize this long document" → summarize-standalone).
  - `nine/registry.py:143` `workflow_gate()` returns `None` for those ids; `nine/cli.py:293` then falls back to `build_default_gate()` (`nine/cli.py:213`), which registers ONLY `eval-json` + `exit-codes`.
  - The 7 EVAL-less lanes never write EVAL.json (grep: analyze 0, compare 0, draft 0, draft-email 0, ideate 0, extract 0, summarize-standalone 0 refs) — their declared hop gates (e.g. analyze docstring `nine/workflows/analyze_wf.py:235`: "INSIGHTS.md non-empty + chart.png >= 1 KiB + artifacts + exits") never run on the single-hop path.
  - In-engine FIX loop (`nine/runtime/workflows.py:868-883`): a FIX verdict re-runs the whole workflow up to `max_fix_loops=2` more times, then `blocked`.
- repro (hermetic):
  ```
  .venv/bin/python - <<'EOF'
  from nine.registry import workflow_gate
  assert workflow_gate("analyze") is None        # hop gate is NOT wired
  # perfect analyze artifact set (INSIGHTS.md + chart.png) through the CLI's fallback gate:
  gate = build_default_gate()
  print(gate.evaluate({...}, dir)["verdict"])     # -> FIX "evidence present but checks failed"
  EOF
  ```
  Live: `nine submit "analyze the sales dataset"` (with GEMINI_API_KEY) → attempt 1 FIX (no EVAL.json) → fix_directive → attempts 2..3 re-run all 4 nodes (2 Gemini calls each) → BLOCK. Deterministic, every time.
- impact: the flagship doc claim "a job is only shipped when the evidence passes — everything else is UNVERIFIED" (README) is unmet for 7 advertised lanes: they can never SHIP and can never produce the EVAL.json the fallback demands. Each submission wastes 2 extra full DAG executions (quota/£). The learn loop records FIX verdicts for lanes that are structurally unfixable → candidate churn that can never be resolved. transform/pipeline/compose/research-deep do write EVAL.json, so they can SHIP, but their hop artifact/output checks are silently skipped too.
- suggested fix: route `workflow_gate()` through `WORKFLOWS[wf_id]().gate_checks` for every registered id (one-line `_HOPS` fallback), or make `build_default_gate()` derive from the hop definition.

## FINDING 2
- area: server API / gate parity
- severity: high
- title: POST /v1/submit always uses the 2-check generic gate — the SAME job can SHIP via the API and FIX via the CLI (hop artifact checks never run server-side)
- evidence:
  - `deploy/server.py:386` `build_gate()` registers only `eval-json` + `exit-codes`; `deploy/server.py:426` uses it for EVERY workflow id, including the 13 `_HOPS` lanes whose CLI gate also enforces required artifacts (e.g. build: `solution.py` + `EVAL.json`).
  - CLI path (`nine/cli.py:293`) uses `workflow_gate(id)` — the hop's full gate.
- repro (hermetic, identical job dir):
  ```
  # job dir: EVAL.json {"checks":[{"name":"self-test","passed":true}]}  — NO solution.py
  server build_gate().evaluate(...)      -> SHIP "all evidence checks passed"
  workflow_gate("build").evaluate(...)   -> FIX "missing artifacts: ['solution.py']"
  ```
- impact: the API can SHIP a "build" job with no solution.py (or a research job with no research.md, etc.) — evidence the CLI gate would refuse. The server's SHIP verdicts feed the learn loop as positive evidence, polluting router stats with false positives. Two entry points, two verdicts for the same evidence.
- suggested fix: server should use the same `workflow_gate(id)` dispatch as the CLI (extract to a shared helper).

## FINDING 3
- area: learn loop / route events
- severity: medium
- title: failed-loud submits record NO route event — the LEARN loop is blind to every failure, contradicting README's "Every submit path … appends a route event … and the verdict"
- evidence:
  - CLI: `_record_route_event` is called only after a successful `executor.execute` (`nine/cli.py:304`); the `WorkflowError` handler (`nine/cli.py:298-302`) returns before it. Server: same pattern (`deploy/server.py:502`, called only after execute).
  - README.md:92-94: "Every submit path (CLI, server, chains, direct answers) appends a **route event** to `jobs/events.jsonl` — the task, the routed workflow, the router's confidence, and the verdict."
  - Repro: `nine submit "hello there"` (no key, scratch `--events`) → exit 1, ledger job `failed`, `events.jsonl` empty (verified live).
- impact: the most common mode (no key / model failure / quota exhaustion — see README "routing works without a key, execution never does") produces zero learning signal. `nine learn scan` can never propose fixes for lanes that fail loud; the "route → execute → verify → learn" loop covers only successes.
- suggested fix: record a `verdict: FAILED` route event in the except path (before returning), or in the store boundary.

## FINDING 4
- area: doc-truth
- severity: medium
- title: test-count claims are stale (README badge/repo-layout/roadmap say 252, SUBMISSION.md says 431/431; actual = 452 collected, 447 passed / 5 skipped) and `.env.example` pins a superseded model
- evidence:
  - README.md:4 badge `tests-252%20passing`; README.md:241 `tests/  252 tests (...)`; README.md:246 "with 252 passing tests"; SUBMISSION.md:51 "431/431 tests pass (5 live-gated skips)".
  - Actual: `.venv/bin/python -m pytest --collect-only -q` → `452 tests collected`; full run (this session) → `447 passed, 5 skipped`.
  - `.env.example` `GEMINI_MODEL=gemini-3.5-flash` while `deploy/deploy.sh` and `nine/runtime/responder.py:32` (DEFAULT_MODEL) both use `gemini-3.6-flash`.
- repro: `pytest --collect-only -q | tail -1` vs README/SUBMISSION lines above.
- impact: judge-facing "252 passing" / "431/431" understate the suite by 21 tests (447 vs 431) and the badge is 195 short; a fresh setup copying `.env.example` runs an older model than the tested/pinned one.
- suggested fix: regenerate counts at HEAD (447 passed/5 skipped) and bump `.env.example` to gemini-3.6-flash.

## FINDING 5
- area: gates / operator UX
- severity: low
- title: every submit prints a false "a SHIP will be refused" provenance WARNING — the lazy `.expected` tags are set during evaluate, so SHIP is never actually refused
- evidence:
  - `nine/gates/evidence.py:36` `register_check` warns when `getattr(fn, "expected", None) is None` at REGISTRATION time; `eval_json_check` (`evidence.py:138`) and `file_nonempty_check` (`evidence.py:118`) set `_check.expected` only inside the closure, on first evaluation.
  - The stale-artifact guard (`nine/runtime/workflows.py:696-707`) runs AFTER `evaluate()`, so the tags are present by then.
  - Repro: `nine submit "hello there"` (no key) prints the WARNING once (`response-nonempty`); a stubbed-key respond run SHIPs cleanly despite the warning (verdict SHIP, job `shipped`).
- impact: scary, incorrect stderr on every workflow run — operators learn to ignore provenance warnings, which is exactly the warning class that SHOULD be loud when real. (`exit_codes_check`/`required_artifact_check` tag at factory time; only the two lazy factories are affected.)
- suggested fix: tag `.expected` at factory time (outside the closure), or have `register_check` defer the warning until the guard actually finds an untagged check.

## FINDING 6
- area: CLI error matrix / redaction
- severity: low
- title: `nine discover --status <anything>` silently exits 0 with "0 job(s)" for invalid filters; `nine learn apply`/`revert` print "no candidate None"; redact() leaks space-separated and short credentials
- evidence:
  - `nine discover --status bogus` → exit 0, "0 job(s)" (no validation of the status enum; an invalid filter is indistinguishable from "no matching jobs"). CLI error matrix otherwise clean: missing args exit 2 with usage; unknown job ids exit 1 with one clean line; `nine memory search` without a query exits 1 with usage.
  - `nine learn apply` (no candidate) → stdout "no candidate None" exit 2.
  - `nine/router/classifier.py` redact(): `"my api key is very-secret-value-here"` → UNCHANGED (pattern list has `api[_-]?key` but not `api key` space-form); `"short key sk-abc"` and `"AIza123"` → unchanged (prefix patterns require ≥10 chars of body). (redact() self-documents as "not a security boundary", so this is a hygiene gap, not a boundary claim.)
- repro:
  ```
  nine --ledger /tmp/x/ledger.jsonl discover --status bogus; echo $?   # 0
  nine learn apply; echo $?                                            # 2 "no candidate None"
  python -c "from nine.router.classifier import redact; print(redact('my api key is very-secret-value-here'))"
  ```
- impact: low; status-filter validation and redact() coverage of natural-language credential phrasing are cosmetic/hygiene gaps.
- suggested fix: validate the status filter against the job state enum (exit 2 + usage), phrase learn messages without "None", extend redact alternation to `api key`/`access key`/space forms.

---

**Summary**: 2 high (F1 gate wiring for 11 lanes — 7 never SHIP; F2 server/CLI verdict divergence), 2 medium (F3 learn-loop blind to failures; F4 stale doc counts + model pin), 2 low (F5 false provenance warning on every run; F6 CLI/redact hygiene). All repros hermetic; no repo code touched.
