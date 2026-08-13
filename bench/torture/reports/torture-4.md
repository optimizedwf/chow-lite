# TORTURE-TESTER-4 Report — attack surface: robustness + fixtures

Worker: TORTURE-TESTER-4 (round 3: env handling, bad JSON, missing files, permission errors, CLI edge cases, new fixture ideas)
Repo HEAD: 59c7579 (slice 23). Note: `nine/gates/evidence.py` had uncommitted fixes mid-session (strict-boolean EVAL.json) — findings below are verified against the CURRENT working tree and avoid already-fixed gaps.
All repros run via `.venv/bin/nine` / `.venv/bin/python` from repo root with scratch paths under /tmp (repo restored afterwards; git status clean).

## FINDING 1
- area: bad JSON inputs / ledger durability
- severity: high
- title: One corrupt ledger line bricks every `nine` command with a raw traceback — including submit, so the operator cannot even recover via the CLI
- evidence: `nine/ledger/ledger.py:138-149` (`_load`): `rec = json.loads(line)` (line 144) — only the FileNotFoundError case is handled; any JSONDecodeError or non-dict line propagates out of `JSONLLedger.__init__`. Repro:
  ```
  printf '%s\n' '{"job_id":"j-ok","workflow_id":"respond","status":"submitted"}' 'this is not json' > /tmp/bad.jsonl
  .venv/bin/nine --ledger /tmp/bad.jsonl discover   # -> JSONDecodeError traceback, exit 1
  .venv/bin/nine --ledger /tmp/bad.jsonl status j-ok   # same traceback
  .venv/bin/nine --ledger /tmp/bad.jsonl submit hello  # crashes BEFORE submitting — CLI unusable
  ```
  A syntactically-valid-but-non-dict line (`["not","a","dict"]`) gives `TypeError: list indices must be integers` at ledger.py:145. Same defect class in the other JSONL stores: `nine/memory/graph.py:105` (`json.loads` in `search_context`), `nine/learn/learner.py:84` (`RouteEvent(**json.loads(line))`) and `:113` (`ImprovementCandidate(**json.loads(line))`) — verified: corrupt `memory.jsonl` kills `nine memory search`; corrupt `events.jsonl` kills `nine learn events` (and server `GET /v1/events` → 500). Unwritable ledger path also raises raw `PermissionError` (no OSError handling in `JSONLLedger.__init__`/`_append`).
- impact: One truncated/partial write (crash mid-append) or manual edit permanently bricks the durable ledger; every command — including submit/recover — dies with a raw traceback. Job history appears "gone" (discover/stats unusable). Same for memory/events/candidates stores.
- suggested_fix: defensive line parser: `try: rec = json.loads(line) except (json.JSONDecodeError, TypeError, ValueError): flag line number, append to `<path>.corrupt.jsonl`, continue`. Require `isinstance(rec, dict)` and validate required keys; wrap `__init__`/`_append` OSErrors in `LedgerError` with a clean one-line message (mirrors T2-F7). Regression test: write a ledger with 1 garbage line + 2 valid jobs; assert `discover`/`status`/`submit` work and the garbage line is reported (hermetic, tmp_path fixture).
- effort: S

## FINDING 2
- area: missing files / recover semantics / data loss
- severity: high
- title: `nine recover` on a shipped/cancelled job destroys the shipped artifacts, then crashes with a raw `InvalidTransition` traceback while the ledger still claims `shipped`
- evidence: `nine/cli.py:339-372`: `ledger.recover()` (`nine/ledger/ledger.py:194-199`) only transitions `blocked`/`failed`; for ANY other status it returns the job unchanged, then `cmd_recover` unconditionally wipes the job dir (`cli.py:364-369`, `p.unlink()` / `shutil.rmtree`) and re-executes, where `job.transition("running")` (`nine/runtime/workflows.py:210`) raises `InvalidTransition: illegal transition shipped -> running`. Repro (fully verified):
  ```
  # ledger line: {"job_id":"j-shipped","workflow_id":"respond","status":"shipped",...}
  # work/j-shipped/task.txt + work/j-shipped/RESPONSE.md present
  .venv/bin/nine --ledger /tmp/shipped.jsonl recover j-shipped --workdir /tmp/work
  # -> "recovering j-shipped (respond) — re-executing"
  # -> RESPONSE.md DELETED (job dir wiped)
  # -> Traceback ... InvalidTransition: illegal transition shipped -> running, exit 1
  # -> last ledger line still "status": "shipped"
  ```
- impact: Shipped artifacts (the verified output of a completed job) are destroyed by a single mistyped command; the ledger then LIES — status `shipped` while the artifact is gone. Same for `cancelled` (and `awaiting_evidence`/`running`). Raw traceback contradicts the clean-error pattern fixed in T2-F7.
- suggested_fix: gate in `cmd_recover` (or `ledger.recover`): only `blocked`/`failed` may be recovered; any other status → clean `error: job <id> is <status>, only blocked/failed can be recovered`, exit 1, BEFORE the wipe. Regression test: seed a shipped job + artifact, run recover, assert exit != 0, no traceback, artifact still exists, ledger line unchanged.
- effort: S

## FINDING 3
- area: bad JSON inputs / config durability
- severity: high
- title: A corrupt `nine/router/catalog.json` (invalid JSON) bricks EVERY `nine` command at import time with a traceback
- evidence: `nine/registry.py:64`: `data = json.loads(_CATALOG_PATH.read_text())` — only `FileNotFoundError` is caught (line 65); `load_catalog()` is called from module level via `KEYWORDS = _merged_keywords()` (registry.py:264), so `import nine.registry` fails when catalog.json is invalid JSON — and cli.py, server.py, chain.py, learner.py all import it. Verified at function level (read-only): with `_CATALOG_PATH` pointed at `{"keyword_overrides": {` (truncated), `load_catalog()` raises `JSONDecodeError` and `_merged_keywords()` propagates it. README explicitly documents manual catalog edits ("edit nine/router/catalog.json manually, then nine learn apply") and `nine learn apply` (`cli.py:472` `save_catalog(catalog)`) writes it non-atomically — a crash mid-write or a bad manual edit kills the whole CLI.
- impact: Total CLI outage with a raw traceback from any subcommand (`discover`, `status`, `stats`, even `--help`-adjacent paths); no graceful degradation, and no way to `learn revert` since learn is also dead. Same failure would take the FastAPI server down (server imports nine.registry).
- suggested_fix: catch `(json.JSONDecodeError, OSError)` in `load_catalog()` → print a loud warning to stderr and return `{}` (routing still works on base keywords); optionally quarantine the bad file. Regression test: point `_CATALOG_PATH` at a corrupt file, assert `load_catalog() == {}` and `KEYWORDS` still contains base keywords.
- effort: S

## FINDING 4
- area: env handling / secrets hygiene (redaction regression)
- severity: high
- title: Task redaction is applied on only ONE of three submit paths — `nine chain` and `POST /v1/submit` store raw task text (incl. credentials) in the durable ledger
- evidence: `nine/cli.py:270` (`cmd_submit`): `input={"task": redact(args.task)}` — the ONLY redacted path (T2-F6 fix). `nine/cli.py:146` (`cmd_chain`): `job = ledger.submit(chain.id, input={"task": args.task})` — raw. `deploy/server.py:262` (POST /v1/submit): `job = ledger.submit(workflow_id=..., input={"task": task})` — raw (server.py never imports `redact`; cf. import at server.py:34). Repro (verified end-to-end, demo chain is bash-only so it runs keyless):
  ```
  .venv/bin/nine --ledger /tmp/chain_led.jsonl --events /tmp/ev.jsonl chain inbox-triage-task-report "the customer password is hunter2 xyz"
  grep hunter2 /tmp/chain_led.jsonl   # -> raw secret persisted in ledger line
  ```
  The `cli.py:354` comment ("ledger input is redacted for display") is therefore false for chain and server paths. Server auth is off by default (`NINE_API_KEY` unset = open, server.py:253-259), so any caller can POST a secret and read it back via `GET /v1/jobs/{id}` (`input` is returned raw — server.py:351-353 `job_detail` returns `job.to_dict()`).
- impact: Credential/secret leakage in the durable ledger + API responses on two of three submit surfaces — a regression of T2-F6 that contradicts the redact-at-boundary doctrine; secrets also flow into backup/snapshot exports (`ledger.snapshot`).
- suggested_fix: redact at the ledger boundary, not per-call-site: `JSONLLedger.submit` (or `Job.__init__`) applies `redact()` to `input["task"]` once (idempotent), and server/chain stop pre-redacting. Regression test: submit via chain with `password=supersecret` task, assert ledger line contains `***` and not the secret.
- effort: S

## FINDING 5
- area: env handling (GEMINI_API_KEY whitespace)
- severity: medium
- title: Whitespace `GEMINI_API_KEY` passes every key guard — jobs burn 3 doomed API retries and report a confusing auth error instead of the documented "requires GEMINI_API_KEY" fail-loud
- evidence: All guards use `if not key` / `if not os.environ.get(...)`, which whitespace defeats: `nine/runtime/responder.py:43-44` (`key = os.environ.get("GEMINI_API_KEY", ""); if not key:`), `nine/cli.py:55` (`_routing_model`), `nine/workflows/test_wf.py:41`, `nine/chains/flagship.py:35,115,192` (research/plan/build). Verified with an injected fake `google.genai` (no network): with `GEMINI_API_KEY="   "`, `respond_text("hi")` does NOT raise the documented WorkflowError and constructs `genai.Client(api_key="   ")`; `_routing_model()` returns a model for the whitespace key. At runtime the doomed call is retried 3× (executor retry/backoff, workflows.py:159-170) before failing with `node respond failed after 3 attempts: ...` — never the clean "GEMINI_API_KEY missing — no offline fallback" message the docs promise.
- impact: Per-job wasted API calls + latency on every submit when the key is set-but-whitespace (a common shell `export KEY=" "` / `.env` mistake); misleading diagnostics; contradicts the documented model-or-fail contract. Also burns a router call per submit (`_routing_model`).
- suggested_fix: `if not key.strip():` in responder.py:44, cli.py:55, test_wf.py:41, flagship.py:35/115/192 (one shared `env_key()` helper). Regression test: run respond workflow with `GEMINI_API_KEY="   "`, assert WorkflowError mentioning GEMINI_API_KEY and zero client constructions (monkeypatch genai).
- effort: S

## FINDING 6
- area: CLI parse edge cases / flag semantics
- severity: medium
- title: Global `--ledger` is silently ignored by `submit` and `chain` (subparser redefinition clobbers it) — scratch/sandbox jobs land in the production ledger with zero warning
- evidence: `nine/cli.py:576` defines global `--ledger`; `submit` (line 583) and `chain` (line 590) RE-declare `--ledger` with the same default, and argparse subparser defaults overwrite values parsed before the subcommand. Verified with a minimal replica of the parser:
  ```
  parse(["--ledger","/tmp/X.jsonl","submit","t1"]) -> ledger = jobs/ledger.jsonl   # global value LOST
  parse(["submit","--ledger","/tmp/Y.jsonl","t2"]) -> ledger = /tmp/Y.jsonl        # only post-subcommand form works
  ```
  Real-world confirmation during testing: `nine --ledger /tmp/... submit ...` and `nine --ledger /tmp/... chain ...` appended jobs to the repo's default `jobs/ledger.jsonl` instead of the requested path (verified, then cleaned up). `status`/`discover`/`cancel`/`stats`/`recover` do NOT re-declare it, so the global flag works there — behavior is inconsistent per subcommand.
- impact: Scripts/CI that sandbox with `nine --ledger /tmp/scratch submit "..."` silently write into the real ledger: polluted `discover`/`stats`, jobs appearing in the wrong store, and (with finding 4) secrets in the wrong durable log. Silent data misdirection with no warning.
- suggested_fix: in the `submit`/`chain` subparsers use `default=argparse.SUPPRESS` for `--ledger` (and `--workdir`) so a pre-subcommand global value survives while post-subcommand placement still works; add a parser unit test asserting both orderings. (Alternative: `parents=[common]` for the flags.)
- effort: S

## FINDING 7
- area: fixtures (new bench ideas)
- severity: low
- title: New bench fixtures bugfix-small-006/007/008 — strict-JSON output, empty/whitespace/unicode input, and missing-env fail-loud
- evidence: Existing fixtures (bugfix-small-001..005, e.g. bench/fixtures/bugfix-small-001/{task.md, starter/solution.py, tests/check.sh}) all exercise pure-Python logic bugs with `check.sh` harness; none exercise robustness (this round's torture found 6 robustness gaps, so the bench has no coverage that would catch their regressions).
  - bugfix-small-006 `strict-json-output`: starter `solution.py` emits JSON via `json.dumps` but must round-trip under STRICT validation; check.sh asserts `json.load` succeeds AND `checks[].passed is True` (rejects `"passed": "false"` strings, `1/0`, missing keys, trailing commas, NaN). Why: regression-guards the EVAL.json gate contract (exactly the class fixed in the working tree mid-round) and catches models/agents emitting sloppy JSON.
  - bugfix-small-007 `empty-and-whitespace-input`: starter function must handle `""`, `"   "`, and unicode (e.g. `"héllo ✓ — テスト"`) without raising or hanging; check.sh feeds those as args/stdin. Why: catches CLI/parser edge cases like the argparse clobber (F6) and empty-task handling; unicode path was manually verified OK today but is untested by the bench.
  - bugfix-small-008 `missing-env-degradation`: starter program must read an env var and fail LOUD with a clean, one-line message when absent/whitespace (no traceback, no retry loop, no silent default). check.sh runs it with the var unset, empty, and `"   "`, asserting exit code + stderr shape. Why: encodes the model-or-fail / fail-loud doctrine from F5 and the clean-error pattern from T2-F7 so future regressions are caught hermetically (no API key needed).
- impact: Bench currently cannot detect regressions in the exact robustness properties that produced findings 1-6; adding these fixtures turns each future regression into a failing check.sh.
- suggested_fix: add the three fixtures under `bench/fixtures/bugfix-small-00{6,7,8}/` following the 001 layout (task.md + starter/solution.py + tests/check.sh + rubric.json), with check.sh runnable standalone and via bench_nine.py.
- effort: M

---
Summary: 6 code gaps (4 high, 2 medium) + 3 fixture proposals, all evidence-backed with reproducible commands. All fixes are <30 lines with hermetic regression-test ideas.
