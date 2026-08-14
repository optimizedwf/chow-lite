# TORTURE-TESTER-16 Report — round 8 (respawn): cancel-vs-LEARN crash, schema depth (format/artifacts), redact edges, deploy auth, rate limiter, submit/recover error matrix, doc-truth

Worker: TORTURE-TESTER-16 (round 8, respawn). Repo HEAD: ce658e5 (slice 33).
NOTE: torture-15.md was found already on disk when I started (the interrupted worker's report
DID land — 13 findings). I deduped against it before filing; T15 covered content-digest edges,
_register manifest ignores, memory-summary misattribution, NON_ROUTABLE_IDS exact-match,
bench pid-file cleanup, convert_to_pytest inlining, and server LedgerUnavailable/fetch-latch
(/v1/events). The findings below are NEW against both the 113-row LEDGER and T15 — where a
surface is adjacent to a T15 finding the T15 id is cited so triage can merge.

All repros hermetic (no Gemini, no network, no quota): `.venv/bin/python` scripts under
/tmp/torture16/ (repro_cancel.py, repro_format.py, repro_artifacts.py, repro_redact.py,
repro_events_badpath.py, repro_git.py, repro_server.py, repro_redact_hold.py), every repro
uses a temp NINE_DATA_DIR/NINE_PLUGIN_REGISTRY. No repo files were modified by me; the only
repo write of mine is this report.
HYGIENE NOTE: during the session a FOREIGN process (triage for torture-15 / slice-34 fixes)
modified `nine/runtime/workflows.py` in the working tree (implements T15-F2/F3/F4/F9:
`_is_ignored` predicate, pid spawn-time + `_prune_node_pid`, snapshot read_bytes OSError
guard). Verified my cited lines are unaffected (`_abort_cancelled` workflows.py:214-240
unchanged; all other cited files untouched). Left the foreign work in place. Final
`git status --short` at report time: `M bench/state.json` (pre-existing),
`M nine/runtime/workflows.py` + `M bench/bench_nine.py` (foreign — slice-34 triage in
progress), `?? bench/torture/reports/torture-15.md` (foreign), `?? bench/torture/reports/torture-16.md` (mine).

Surfaces that HOLD after re-attack (verified today, not re-filed):
- spec-014 redact battery (9 shapes): API_KEY=/password:/password ==/JSON-quoted/AKIA/xox/
  Bearer/PRIVATE KEY all redact correctly (repro_redact_hold.py) — the NEW defects are
  over-redaction + specific tail/space leak edges (Finding 4), not the base battery.
- Server body cap: oversized/chunked body → clean 413 (repro_server.py; T7-F6 holds), and the
  content-length fast path still caps before buffering.
- Firestore fallback: missing creds → LOUD printed warning + JSONL fallback + clean 502s
  (repro_server.py output; T14-F10 holds for the paths T15 did not re-open).
- Ledger-boundary redaction (T4-F4): submit stores redact()ed task; chain + demo_live both
  route through ledger.submit (re-read).
- Chain path is CANCELLED-safe: ChainExecutor checks CANCELLED before learner.observe
  (nine/chains/chain.py:222-229) — the Finding-1 crash is workflow-path-only.
- T14-F6 verdict response shape for chain routes (`{"verdict": {"verdict":…, "hops":…}}`),
  T15 holds list (content-digest core, _register same-name replace, gate symlink stance,
  NON_ROUTABLE_IDS core, convert_to_pytest core, pid-file core) — re-read, no regression on
  the surfaces I touched.

---

## FINDING 1
- area: deploy/LEARN end-to-end / router substrate (cancel race) / CLI + API error matrix
- severity: medium
- title: Operator-cancel mid-run crashes `nine submit`/`nine recover`/POST /v1/submit with a RAW traceback — `_abort_cancelled` stamps a `CANCELLED` verdict that violates the route-event schema, so `_record_route_event` raises SchemaValidationError (a ValueError) which `except WorkflowError` never catches; the cancelled run's route event is lost
- evidence: `nine/runtime/workflows.py:258-288` (line 214-240 at HEAD ce658e5; the working
  tree shifted it — foreign slice-34 edits, see hygiene note; content UNCHANGED) —
  `_abort_cancelled` returns `{"verdict": "CANCELLED", …}` with NO `gate_version`; `nine/cli.py:283` `_execute_job` calls `_record_route_event(learner, job, decision, result["verdict"])` unconditionally; `nine/cli.py:338-359` builds `RouteEvent(verdict=…)`; `nine/learn/learner.py:76` `validate("route-event", …)` → `SchemaValidationError`; schemas: `route-event` enum is SHIP/FIX/BLOCK/UNVERIFIED, `evidence-verdict` enum SHIP/FIX/BLOCK + required gate_version (no CANCELLED). `cmd_submit` (cli.py:331) and `cmd_recover` (cli.py:512) catch ONLY `WorkflowError`; server `submit` (deploy/server.py:351-395) has no handler for it either (WorkflowError handler at :120 covers only WorkflowError). Chain path is safe (chain.py checks CANCELLED pre-observe). Repro `/tmp/torture16/repro_cancel.py`:
  ```
  [2] _record_route_event raised: SchemaValidationError | route-event schema violation:
      'CANCELLED' is not one of ['SHIP', 'FIX', 'BLOCK', 'UNVERIFIED'] (path: ['verdict'])
      is ValueError (NOT WorkflowError)? True
  [3] ledger final status: cancelled | verdicts: []   (durable verdicts stay [] — in-memory only)
  ```
- impact: a cancel during a workflow run (CLI Ctrl-C path or API-visible cancel, or the demo's own cancel button racing a submit) raw-tracebacks the caller instead of a clean line; the LEARN loop permanently misses that run's observation (route event lost); CI/scripts keying on a clean exit get a non-zero traceback for a legitimate cancel. No durable schema violation (verdict is in-memory only) — the crash + lost event are the defect.
- suggested fix: in `_record_route_event` (or before it, cli.py:283), skip recording when `verdict.get("verdict") == "CANCELLED"` (nothing to learn — the run never completed), OR add `CANCELLED` to the route-event enum with a distinct `candidate`-suppression path. Belt: `cmd_submit`/`cmd_recover`/server `submit` catch `(WorkflowError, ValueError)` and print one clean line. Regression-test idea: hermetic workflow with a bash `sleep` node → `ledger.cancel` mid-run → `_execute_job` returns a clean line and events.jsonl has NO cancelled event.
- effort: S

## FINDING 2
- area: schema_validation depth (schemas/ all 5)
- severity: low
- title: `format: date-time` is a dead constraint in every schema — `Draft202012Validator` is built without a format checker, so garbage timestamps pass validate() for route-event/agent-job/route-decision/artifact-manifest
- evidence: `nine/schema_validation.py:49` constructs the validator with no `format_checker`; `schemas/route-event.schema.json` `recorded_at` = `{"type":"string","format":"date-time"}` (same in agent-job `created_at/updated_at/completed_at`, route-decision `decided_at`, artifact-manifest `produced_at`). Repro `/tmp/torture16/repro_format.py`:
  ```
  (a) route-event recorded_at='yesterday-not-a-date' accepted: True
  (b) agent-job created_at='not-a-date' accepted: True
  (c) route-decision decided_at='whenever' accepted: True
  (e) format checker installed: None
  ```
- impact: the schema contract that "every boundary is typed" silently accepts non-timestamps; downstream consumers (operators, the API, future analytics) that parse `recorded_at`/`created_at` get ValueError/None for garbage that validation claimed to reject. Low — display/parse robustness only.
- suggested fix: `Draft202012Validator(schema, registry=_REGISTRY, format_checker=FormatChecker())` (one line; `jsonschema.FormatChecker` is stdlib-lib). Add `format: date-time` regression asserts (garbage strings rejected; ISO-8601 accepted) to the existing schema_validation tests.
- effort: S

## FINDING 3
- area: nine artifacts/evidence replay / ledger mutators / schema_validation depth
- severity: medium
- title: artifact-manifest is validated NOWHERE — `Job.add_artifact`/`add_verdict`/`attach_route_decision` do no boundary validation and the agent-job `$ref`s are only exercised on a FRESH empty job at submit, so a malformed ledger artifact entry loads fine and `nine artifacts`/`nine chain`/demo_live raw-traceback `KeyError: 'name'`
- evidence: `nine/ledger/ledger.py:111-121` — `add_verdict`/`add_artifact`/`attach_route_decision` append blindly; `ledger.py:236` is the ONLY `validate("agent-job", …)` and it runs at submit on a job whose `artifacts=[]`, `verdicts=[]`, `route_decision=None` — the `$ref`s to artifact-manifest/evidence-verdict/route-decision (schemas/agent-job.schema.json) are never exercised for appended data; grep: exactly 4 `validate(` sites in nine/ (evidence.py:74, ledger.py:236, learner.py:76, classifier.py:286) — artifact-manifest is validated at NONE of them. `_looks_like_job` (ledger.py:124-149) checks only "artifacts is a list". `cmd_artifacts` (cli.py:393) `a['name']`, `cmd_chain` (cli.py:202) and demo_live.py:85 access `a['name']/['sha256']/['size']/['produced_by']` with only `LedgerError` caught. Repro `/tmp/torture16/repro_artifacts.py`:
  ```
  (a) add_artifact({'name': 'only-a-name'}) accepted: True (no boundary validation)
      is_valid('artifact-manifest', entry): False
  (b) _looks_like_job accepts artifacts=[{'foo': 1}]: True
      cmd_artifacts line 393 -> KeyError: 'name' (raw traceback; only LedgerError is caught)
  (c) kind='binary-exe' accepted by add_artifact; is_valid: False
  ```
- impact: any producer (plugin, hand-edit, foreign writer, future tool node) that writes a non-conforming artifact entry turns every read path into a raw traceback — the T6-F3 list-level guard was never extended to entry shapes, and the "typed schemas for every boundary" claim (SUBMISSION.md) is false for the entire post-submit mutation surface.
- suggested fix: validate in the three mutators (validate "artifact-manifest"/"evidence-verdict"/"route-decision" on the entry before append) and re-validate the assembled job in `update()`; harden `_looks_like_job` to check entry shapes (or rely on update-time validation); make the three print sites tolerate missing keys with one clean line. Regression-test idea: `add_artifact({"foo": 1})` raises a typed error; a ledger line with `artifacts:[{"foo":1}]` makes `nine artifacts` print one clean error, exit 1.
- effort: S

## FINDING 4
- area: router substrate (`redact()` in nine/router/classifier.py) / memory + ledger durable text
- severity: low
- title: `redact()` over-redacts innocent words into durable records AND still leaks three shapes — `(sk|pk|…)[A-Za-z0-9_-]{10,}` with IGNORECASE and no word boundary turns `skillfulness`/`skateboarding` into `sk***` (task text corrupted on every submit path), while `password == hunter2 == hunter3` leaks `== hunter3`, JSON-quoted values with spaces (`"token": "sk-123 abc"`) leak whole, and `api_key = "sk-123 abc def"` leaks `abc def"`; pattern lines 57 and 69 are byte-identical duplicates
- evidence: `nine/router/classifier.py:72` `(sk|pk|ghp|gho|AIza)[A-Za-z0-9_\-]{10,}` re.IGNORECASE, no `\b`; :57 vs :69 identical `(?:^|[\s=:])…` comparison-tail pattern; :61 JSON-quoted pattern uses `\S+` so a space ends the match. Durable impact: `ledger.submit` (ledger.py:230-234) redacts the task into `route_decision.task_redacted` and the ledger `input`, so over-redaction permanently corrupts what operators/API consumers see. Repro `/tmp/torture16/repro_redact.py`:
  ```
  'skillfulness'                      -> 'sk***'
  'skateboarding'                     -> 'sk***'
  'password == hunter2 == hunter3'    -> 'password=*** == hunter3'
  '"token": "sk-123 abc"'             -> '"token": "sk-123 abc"'
  'api_key = "sk-123 abc def"'        -> 'api_key=*** abc def"'
  ```
- impact: legitimate task text ("skillfulness", "skateboarding", "pkill"-family words don't match but any 10+ char sk-word does) is destroyed in the durable record — worse than a leak for operators debugging a routed task; conversely the comparison-chain tail and space-containing JSON values leak real secrets into the same records. (T15-F6 covers the URL-userinfo / `--password` / `Authorization: Basic` leak shapes — adjacent, same function; this finding's shapes are disjoint.)
- suggested fix: add `\b` before the `(sk|pk|…)` alternation and drop IGNORECASE for it (or require a `[=:]`/quoted context); extend the comparison-tail pattern to consume `(?:\s*(?:==|!=|is|was|=)\s*\S+)*`; make the JSON-quoted value pattern tolerate spaces (match until the closing quote); delete the duplicate line 69. Regression-test idea: extend the spec-014 battery with the 5 repro shapes + "skillfulness"/"skateboarding" (must pass through unchanged).
- effort: S

## FINDING 5
- area: deploy server auth/rate/bounds (deploy/deploy.sh + deploy/cloud-run.yaml + deploy/server.py)
- severity: medium
- title: the canonical deploy recipe ships a PUBLIC unauthenticated API — `--allow-unauthenticated` with no `NINE_API_KEY` anywhere (deploy.sh or cloud-run.yaml), so `_check_auth` is dead in the documented production path; with the GEMINI_API_KEY secret attached (the documented live path) anyone can POST /v1/submit and burn the operator's paid Gemini quota, and read every job record
- evidence: `deploy/deploy.sh:35` `--allow-unauthenticated`; deploy.sh:36 `--set-env-vars GEMINI_MODEL=…,FIRESTORE_COLLECTION=…,NINE_MEMORY=firestore` + optional `--set-secrets GEMINI_API_KEY=…` — no NINE_API_KEY; `deploy/cloud-run.yaml` env block has no NINE_API_KEY; `deploy/server.py:269` `_API_KEY = os.environ.get("NINE_API_KEY", "")` (module-load, default open), `:275-280` `_check_auth` no-ops when unset. Repro `/tmp/torture16/repro_server.py`:
  ```
  _API_KEY value when NINE_API_KEY unset: ''
  (1) POST /v1/submit with NO auth header -> status: 502 | detail: node summarize-SOURCE failed:
      summarize requires an LLM key (gemini: GEMINI_API_KEY; opena…
      auth gate fired?  False        <- no 401; request proceeded into job execution
  (2) with NINE_API_KEY set, no header -> status: 401 ; wrong key -> 401 ; right key -> 502
  ```
  The 502 is the fail-loud doctrine (model-or-fail) — the point is the auth gate never fired; with GEMINI_API_KEY present (the documented live deployment) the same anonymous POST runs a real paid Gemini job. GET /v1/jobs + /v1/jobs/{id} + /v1/events are equally open → full read of every task (redacted), verdict, route decision, event.
- impact: cost DoS (30 req/min/IP × 2 instances × up to 2000-char tasks on the operator's Gemini bill) + disclosure of job/event records to anyone with the URL. The server docstring's "Set it before deploying publicly" (server.py:276-277) is the only warning — the deploy recipe never offers the knob.
- suggested fix: make deploy.sh REQUIRE a NINE_API_KEY secret (create it with `gcloud secrets` + `--set-secrets NINE_API_KEY=…` when missing, or `--no-allow-unauthenticated` + a Service Account / IAP), and document the pairing; keep `_API_KEY` as the dev default but add a startup warning when the server boots with no key and `K_SERVICE` is set. Regression-test idea: deploy.sh dry-run asserts the NINE_API_KEY secret is wired whenever `--allow-unauthenticated` is present.
- effort: S

## FINDING 6
- area: deploy server auth/rate/bounds (deploy/server.py)
- severity: low
- title: rate-limit bookkeeping grows UNBOUNDED — `_hits` is a `defaultdict(deque)` whose entries are only pruned when the SAME IP returns, so every single-request IP (scanner, attacker rotating IPs, or just many distinct users) leaves a permanent deque entry; memory grows without bound on the 512MiB Cloud Run container, and every /v1/ path (including GET) appends
- evidence: `deploy/server.py:272` `_hits: dict[str, deque] = defaultdict(deque)`; `:283-292` `_check_rate_limit` prunes `q` only for the CURRENT ip, appends `now`; `:295-309` `_guard` calls it for every path starting `/v1/` (GET /v1/jobs, /v1/stats, /v1/events included) and BEFORE auth (`:303-305` rate → `:306-308` auth), so unauthenticated traffic also populates the table. Repro (repro_server.py): `_hits` grew 1 entry per new IP with no eviction path; same-IP deque holds every request within the window (8 entries after 8 requests).
- impact: slow-motion OOM under distributed IPs (each IP's deque entry persists forever) — a memory DoS on the documented deployment; also the rate→auth order means a correct X-API-Key caller shares quota with anonymous scanners (minor).
- suggested fix: evict idle entries — either a periodic sweep (e.g., on each request, drop IPs whose deque is empty/older than window from the dict) or switch to a fixed-size LRU (e.g., `OrderedDict` capped at N IPs); move auth BEFORE rate-limit so bad keys never consume quota. Regression-test idea: simulate 10k distinct single-hit IPs → `len(_hits)` stays bounded after a sweep.
- effort: S

## FINDING 7
- area: CLI one-line-error matrix (`nine submit`/`nine recover`) / deploy LEARN
- severity: low
- title: T14-F7's store-construction guard missed the submit/recover surface — `_execute_job` builds the learner at cli.py:223 with no OSError wrapper, so `nine submit --events <path whose parent is a file>` (and `nine recover`) raw-tracebacks FileExistsError; the adjacent `job_dir.mkdir` at cli.py:242 is equally unwrapped (a `work` FILE in cwd raw-tracebacks on submit/recover/chain/server)
- evidence: `nine/cli.py:223` `learner = _learner(args)` inside `_execute_job` (shared by cmd_submit:330 and cmd_recover:511); T14-F7 wrapped the store construction ONLY in `cmd_learn` (:551) and `cmd_chain` (:168-177); `_learner` (:530+) → `RouteEventStore(path)` → `path.parent.mkdir(parents=True, exist_ok=True)` (learner.py) raises FileExistsError when a parent component is a FILE. Repro `/tmp/torture16/repro_events_badpath.py` (`.venv/bin/python`, temp NINE_DATA_DIR):
  ```
  raised: FileExistsError | [Errno 17] File exists: '/tmp/torture16/data3/blocker'
  tail: … pathlib.py, line 1311, in mkdir → os.mkdir(self, mode) → FileExistsError: …
  ```
  Adjacent: `_execute_job` cli.py:241-242 `job_dir.mkdir(parents=True, exist_ok=True)` (also cmd_chain:181-182, server submit:373) — same FileExistsError family, unwrapped.
- impact: a typo'd --events path or a `work` file in cwd turns submit/recover into a raw traceback instead of one clean line — exactly the class T14-F7/T12-F8 set out to close on the other surfaces.
- suggested fix: wrap `_learner(args)` in `_execute_job` with `except OSError → print one clean line, return 1` (mirror cmd_chain:168-177), and wrap the `job_dir.mkdir` call sites the same way; add a regression test submitting with `--events data3/blocker/x` where `blocker` is a file → clean error, exit 1.
- effort: S

## FINDING 8
- area: README/SUBMISSION doc-truth vs code
- severity: low
- title: doc-truth stale again — README claims "252 tests / 80% coverage" while pytest collects 414 (the T5-F8 sync and the T8-F8 sweep missed the count drift and SUBMISSION.md); SUBMISSION.md still advertises "Gemini 3.5 Flash" (three sites) and "99/99 tests pass" while the code serves `gemini-3.6-flash` and collects 414
- evidence: `pytest --collect-only -q` in repo cwd today → **414 tests collected** (returncode 0, no cache pollution; git status unchanged). README.md:4 `tests-252%20passing` badge, :5 `coverage-80%25` badge, :241 `tests/ 252 tests`, :246 `252 passing tests`. SUBMISSION.md:15 `Google ADK 2 + Gemini 3.5 Flash`, :29 `a Gemini 3.5 Flash router`, :97 `Gemini 3.5 + Cloud Run/Firestore` vs `nine/runtime/llm_provider.py:29` `GEMINI_DEFAULT_MODEL="gemini-3.6-flash"` (and README:194 correctly says 3.6 Flash); SUBMISSION.md:51 `99/99 tests pass (5 live-gated skips)` and :98 `99 tests`.
- impact: a grader/judge (or the user) reading the submission docs sees "Gemini 3.5" and "252/99 tests" — the stale count is the same failure T5-F8 was filed for; the model-name drift is exactly what T8-F8's sweep was supposed to catch but never touched SUBMISSION.md.
- suggested fix: re-run the count at harvest time and regenerate README badge + `tests/` line (or drop the literal count in favor of a CI-generated value); sweep SUBMISSION.md for `Gemini 3.5` → 3.6 and re-derive the test total from pytest. Regression-test idea: a doc-truth check in the harness that greps README/SUBMISSION for model names and runs `pytest --collect-only` to compare counts.
- effort: S

## FINDING 9
- area: CLI one-line-error matrix (`nine learn apply`/`revert`) / deploy robustness
- severity: low
- title: `_git_commit` raw-tracebacks `CalledProcessError` and leaves divergent state — `subprocess.run(..., check=True)` with no handler, so on any non-git deployment (pip/sdist install, tarball, fresh container) `nine learn apply`/`revert` crashes AFTER `save_catalog()` already mutated catalog.json and BEFORE `update_status()` flips the candidate (catalog changed, candidate still pending)
- evidence: `nine/cli.py:746-753` `_git_commit` — two `_sp.run(..., check=True)` calls, no try/except; called at :655 (apply) and :700 (revert), each AFTER `save_catalog` (:645/:698) and BEFORE `update_status` (:656/:701). Repro `/tmp/torture16/repro_git.py` (same invocation, `-C` a non-git dir):
  ```
  _git_commit add would raise CalledProcessError, rc: 128
    stderr: fatal: not a git repository (or any of the parent directories): .git
  ```
- impact: on any deployment without a `.git` at the repo root (the Dockerfile/Cloud Run image builds, pip installs), a catalog keyword apply mutates the catalog on disk, then dies with a raw traceback and no status update — the operator sees a failed command yet the catalog changed (silent partial mutation); CI on the dev repo with a pre-commit hook failure hits the same.
- suggested fix: wrap the two git calls in try/except and degrade gracefully: print a loud warning ("catalog changed but not committed (no git repo / commit failed) — commit manually"), return 1 (or a distinct code) WITHOUT leaving the candidate marked applied; better: commit FIRST or make the apply transactional (write + verify git + update status). Regression-test idea: monkeypatch `_sp.run` to raise CalledProcessError → apply returns 1, catalog unchanged-or-warned, candidate status not "applied".
- effort: S
