# TORTURE-25 — Robustness + Fixtures (CLI/env edge hunt), round 13

**Repo**: chow-lite · **Round**: 13 (torture-25) · **Surface**: robustness +
fixtures — env handling (junk/edge env values), bad JSON / corrupt ledger
lines, missing/unreadable files, permission errors, CLI error paths (clean
one-line / non-zero exit contract), recover/cancel edge cases, fixture spec
proposals (011+).

**Method**: read-only static exploration + hermetic repros
(`.venv/bin/python`, zero Gemini quota, no git touches). No repo file
modified except this report. All repros ran in /tmp/tt25 with throwaway
ledgers/job dirs.

**Baseline**: slices 43-47 shipped T21-F1..F6, T22-F1..F3, T23-F1..F3,
T24-F1..F5 (gate FIFO/hang guards, aux-write best-effort, timeout-env
validation before submit, CLI OSError belts, recover wipe ordering,
task.txt UTF-8/FIFO refusal, junk-env warn-and-fallback convention). This
round hunts NEW gaps at the edges of those fixes.

Findings: 5 (1 medium, 4 low).

---

## FINDING 1
- area: CLI
- severity: medium
- title: `nine submit`/`nine chain` raw-traceback `LedgerError` when the ledger file is not appendable — the primary submit path is outside every try
- evidence: nine/cli.py:422 (`job = ledger.submit(...)`) and nine/cli.py:213 (`cmd_chain`) sit OUTSIDE the `except LedgerError` guards; `main()` (cli.py:1073-1078) has no global handler. `_append` wraps OSError -> LedgerError (nine/ledger/ledger.py:254-257) so ANY append failure (read-only ledger, full disk, chmod 444) raises LedgerError uncaught. Repro (hermetic, /tmp/tt25):
  ```bash
  touch ro.jsonl && chmod 444 ro.jsonl
  .venv/bin/python - <<'EOF'
  import os, sys, traceback
  sys.path.insert(0, "/Users/adam26/chow-work/chow-lite"); os.chdir("/tmp/tt25")
  from nine.cli import main
  try: main(["submit", "--ledger", "/tmp/tt25/ro.jsonl", "--workdir", "/tmp/tt25/work", "hello world task"])
  except BaseException: print(traceback.format_exc()[-400:])
  EOF
  ```
  -> full `nine.ledger.ledger.LedgerError: cannot append to ledger ... Permission denied` traceback at cli.py:422. Every other command (status/discover/cancel/recover, T2-F7/T12-F8/T13-F1) promises ONE clean `error:` line — submit/chain broke it.
- impact: an operator with a full disk or a read-only ledger (or `chmod 444 jobs/ledger.jsonl`) gets a wall of Python instead of one actionable line; scripts/CI can't key on the documented clean-error contract. Exit code is still 1 (Python default), so the non-zero contract holds — this is a clean-error contract violation, not a silent-success.
- suggested_fix: wrap `ledger.submit` (+ `attach_route_decision`/`ledger.update` before `_execute_job`) in `except LedgerError -> print("error: ...", stderr); return 1` in cmd_submit (mirror cmd_chain's `_ledger` guard / T12-F8 pattern). Regression test: chmod 444 ledger + `main([...])` -> return code 1 + stderr line starts with "error:", no Traceback in output.
- effort: S

## FINDING 2
- area: CLI / recover edge
- severity: low
- title: `nine recover` raw-tracebacks PermissionError on an unreadable (chmod 000) task.txt — T23-F3's corrupt-UTF8 guard covers UnicodeDecodeError only, not OSError
- evidence: nine/cli.py:602-612 — `if task_txt.is_file(): task = task_txt.read_text(...)` catches only `UnicodeDecodeError`. A `chmod 000 work/<id>/task.txt` passes `is_file()` and `read_text` raises `PermissionError` (an OSError) uncaught. Repro (hermetic): seed `Job(workflow_id="respond")`, status=blocked, `work/<id>/task.txt` chmod 000, then:
  ```python
  main(["--ledger", "/tmp/tt25/led4.jsonl", "--workdir", "/tmp/tt25/work", "recover", "job-tt25-blocked"])
  ```
  -> `PermissionError: [Errno 13] Permission denied: '.../task.txt'` traceback at cli.py:604. Same family T15-F2 fixed for the gate-exemption path ("unreadable input -> BLOCK, never raw crash") — the recover path was missed. Order is safe (crash is before the wipe/transition, so no tombstone), but the clean-error contract is broken.
- impact: a permission hiccup on the job dir (or a job dir restored from a read-only backup) makes recover dump a raw traceback with a Python-internal message instead of the documented one-line refusal; the operator must guess how to salvage the job.
- suggested_fix: catch OSError alongside UnicodeDecodeError in the task.txt read and refuse with the same clean message as the missing/corrupt paths (job stays blocked/failed). Regression test: chmod 000 task.txt -> rc 1, one `error:` line, no Traceback, ledger row unchanged.
- effort: S

## FINDING 3
- area: robustness / env
- severity: low
- title: `NINE_TASK_CAP` / `NINE_INSTRUCTION_LIMIT` junk values raise raw `ValueError` in the debug lane — violates the established junk-env warn-and-fallback convention (T9-F6/T22-F2/T24-F5)
- evidence: nine/workflows/debug_wf.py:30 `limit = int(_os.environ.get("NINE_INSTRUCTION_LIMIT", "1400"))` and :71/:164 `_task_cap = int(_os.environ.get("NINE_TASK_CAP", "1400"))` have NO ValueError guard. Repro (hermetic):
  ```python
  os.environ["NINE_INSTRUCTION_LIMIT"] = "abc"
  from nine.workflows.debug_wf import _cap_instruction
  _cap_instruction("short", 0)  # -> ValueError: invalid literal for int() with base 10: 'abc'
  ```
  Sibling env parses in the same repo all guard: NINE_MAX_LLM_CALLS (adk_runtime.py:163 + T21-F4/T24-F5 loud warning), NINE_GATE_TIMEOUT_S (workflows.py:263 falls back 60), NINE_MAX_TOKENS (llm_provider.py:466 falls back 4096), NINE_LLM_TIMEOUT_S (llm_provider.py:495 falls back 120). A typo like `NINE_TASK_CAP=2k` (a natural way to write 2000) kills every debug/build lane node with a cryptic `invalid literal` that names no env var — surfaced via cli.py:427 as `[error] job ... failed loud` after the job is durably submitted.
- impact: one typo'd env value turns the whole debug/build lane into immediate failures with a message that gives no hint which variable is wrong; no loud stderr warning, no fallback — the exact user pain the junk-env convention was built to prevent.
- suggested_fix: wrap both int() parses: on ValueError print a one-line stderr WARNING naming the variable + value and fall back to 1400 (mirror T24-F5's NINE_MAX_LLM_CALLS fix); optionally treat <1 the same as junk. Regression test: NINE_TASK_CAP=2k and NINE_INSTRUCTION_LIMIT=abc -> node runs with 1400 cap, warning on stderr.
- effort: S


## FINDING 4
- area: robustness / env
- severity: low
- title: `NINE_LLM_TIMEOUT_S=nan` / `-5` / `inf` slip past the ValueError fallback and kill every model POST with a library-level error — the NaN family T19-F4 closed for bench pids recurs in llm_provider
- evidence: nine/runtime/llm_provider.py:495 `_timeout_s = float(os.environ.get("NINE_LLM_TIMEOUT_S", "120"))` — the try/except catches string-parse ValueError, but `float("nan")`, `float("-5")`, `float("1e400")` all return valid floats. Repro (hermetic):
  ```python
  import requests
  requests.post("http://127.0.0.1:1/x", json={}, timeout=float("nan"))
  # -> ValueError: Invalid value NaN (not a number)
  requests.post("http://127.0.0.1:1/x", json={}, timeout=-5)
  # -> ValueError: Attempted to set connect timeout to -5, but the timeout cannot be set ...
  requests.post("http://127.0.0.1:1/x", json={}, timeout=1e400)
  # -> OverflowError: timestamp out of range for platform time_t
  ```
  The requests.post call at llm_provider.py:511 is NOT inside any try — the ValueError/OverflowError surfaces from the model call site with no mention of the env var (a templated config producing `nan` — the same failure mode T24-F5/T22-F2 warn about — turns every LLM call into an instant failure instead of a loud warning + the documented 120s fallback).
- impact: one junk/edge value in NINE_LLM_TIMEOUT_S makes every model-backed node fail immediately with a third-party urllib3 error; no warning names the offending env var; the operator stares at "Invalid value NaN" while the actual knob is NINE_LLM_TIMEOUT_S. Negative values (plausible as a "disable timeout" attempt) hard-fail too.
- suggested_fix: after the float() parse, reject non-finite (`math.isfinite`) and <1 values with a one-line stderr WARNING naming the var + value, then fall back to 120.0 (mirror `_gate_timeout_s`'s `>= 1 else 60` pattern at workflows.py:263-270). Regression test: env=nan / -5 / inf -> warning printed, timeout param = 120.0 (monkeypatch requests.post and assert).
- effort: S

## FINDING 5
- area: fixtures
- severity: low
- title: New fixture specs bugfix-small-016 + 017 proposed — read-only-ledger clean failure and unreadable-task.txt recover refusal (map 1:1 to FINDING 1 / FINDING 2 invariants)
- evidence: bench fixtures 001-010 ship (bench/state.json), specs 011-015 exist/deferred (LEDGER FIXTURES-009-011 + T14-SPEC). 016/017 are new and disjoint: 016 = "a submit to an un-appendable ledger must fail with ONE clean error line + rc 1 (never a Python traceback)" — exactly the invariant FINDING 1's repro violates today; 017 = "recover must refuse cleanly (rc 1, one error line, ledger row untouched) when task.txt is unreadable (chmod 000) or corrupt" — FINDING 2's invariant. Both are negative-control fixtures in the established shape: starter-broken candidate (raw traceback / error-line absent), fixed-candidate positive, tests/check.sh with discriminating cases (grep the CLI stderr for `error:` and assert rc=1 and no "Traceback"), convert_to_pytest 1:1 path already proven for 10 fixtures.
- impact: without a fixture pinning them, FINDING 1/2 invariants are one refactor away from regression (the exact mechanism that repeatedly re-opened the clean-error contract in T2-F7/T13-F1/T14-F7 — each was closed by a fix but never frozen as a bench fixture).
- suggested_fix: build 016/017 when BENCH is the active lane (same gate as 011-015: 011 atomic-JSONL append, 012 stale-evidence, 013 strict-boolean EVAL, 014 secret redaction, 015 process-group cleanup). 016 needs a shell-created chmod-444 ledger + a submit that must not reach the LLM; 017 needs a seeded blocked job with chmod-000 task.txt.
- effort: M (two fixtures, standard starter/candidate/check.sh scaffold; no new runtime work)

---

**Method note**: all repros executed under `.venv/bin/python` in /tmp/tt25 with throwaway ledgers/job dirs; zero Gemini quota touched (no ADK/model nodes invoked — FINDING 1/2 crash before any model call, FINDING 3/4 run at parse time). No repo files modified except bench/torture/reports/torture-25.md.

**Severity count**: critical 0 · high 0 · medium 1 · low 4.
