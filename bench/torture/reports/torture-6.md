# TORTURE-TESTER-6 Report — attack surface: robustness + fixtures (error paths in NEW code)

Worker: TORTURE-TESTER-6 (round 4: ledger corrupt-line skip edge cases, OSError->LedgerError,
recover refusal, catalog degrade, redact() IGNORECASE edge cases, argparse SUPPRESS fallbacks,
callable-node timeout artifacts, `_contained_write` edge cases, new fixture proposals)
Repo HEAD: 25a87c0 (slice 25 — T4-F7 fixtures bugfix-small-006/007/008 landed mid-session; the
reserved slots are now taken, so fixture proposals below target 009-011).
All repros hermetic (no Gemini, no network): `.venv/bin/python` from repo root, scratch paths
under /tmp, real `catalog.json` backed up/restored byte-identical (git diff clean). Full suite
at HEAD: 252 passed, 5 skipped.

Re-attacked surfaces that HOLD (not re-filed): recover refuses non-blocked/failed cleanly and the
wipe order checks `is_symlink` before `is_dir` so dir-symlinks are unlinked, not rmtree'd
(cli.py:367-369); `_contained_write` write-side refuses symlink escapes and `..` (flagship.py:74-84),
unicode filenames fine, Windows-style separators are a harmless literal filename on POSIX; ledger
status after a callable-node timeout is `failed` (T3-F5); JSON-level corrupt lines skip + count.

## FINDING 1
- area: runtime (evidence integrity — containment READ side; twin of fixed T3-F7 write side)
- severity: high
- title: A job can certify OUTSIDE file content as its own evidence via a symlink — artifact manifest and evidence gates read through symlinks
- evidence: `nine/runtime/workflows.py:288-294` — artifact registration does `if not p.is_file(): continue` then `p.stat()` + `p.read_bytes()`, all of which FOLLOW symlinks; `nine/gates/evidence.py:78-90` (`load_eval_json`) and `:161-166` (`required_artifact_check`) also read/stat through symlinks. Repro (hermetic):
  ```
  # a bash/tool node creates a symlink into the job dir pointing OUTSIDE it
  os.symlink(OTHER_JOB_EVAL_JSON, job_dir/"EVAL.json")
  ```
  → the manifest registers `EVAL.json` with the OUTSIDE file's sha256/size, and the evidence gate reads the outside content and returns SHIP. Verified: artifact `sha256 == sha256(outside file)`, `produced_by` = the node, verdict SHIP. No test covers symlinks (`grep -rn symlink tests/` = empty).
- impact: any bash/tool node (or confused model write step) can `ln -s` another job's EVAL.json (or any file) into its own job dir and the gate certifies content that was never produced in this workspace — cross-job evidence poisoning, manifest lies, and a direct bypass of the "evidence-gated, exit code is not success" doctrine. The T3-F7 fix contained the WRITE path; the READ/registration path was never checked.
- suggested_fix: in the registration loop and gate file checks, treat symlinks as non-evidence: `if p.is_symlink(): continue`, `p.stat(follow_symlinks=False)`, read via `os.open(..., O_NOFOLLOW)`; `required_artifact_check`/`load_eval_json` should refuse symlinked EVAL.json (or the executor should refuse to run a workflow whose job dir contains symlinks). Regression test: seed job dir with `EVAL.json -> outside file` symlink → assert manifest excludes it AND gate verdict is not SHIP. 
- effort: S

## FINDING 2
- area: robustness (ledger corrupt-line skip is byte-level blind)
- severity: high
- title: One non-UTF8 byte in the ledger bricks EVERY `nine` command with a raw traceback — and the OSError->LedgerError wrap misses the constructor's mkdir
- evidence: `nine/ledger/ledger.py:148` — `self.path.read_text()` decodes the WHOLE file before the per-line JSON try/except; a single invalid byte raises `UnicodeDecodeError`, which is neither JSONDecodeError nor the OSError caught at `:142-143`. `:134` — `self.path.parent.mkdir(...)` sits OUTSIDE that try, so a FileExistsError (parent path is a file) also escapes as a raw traceback. Repro:
  ```
  printf '{"job_id":"j1","workflow_id":"respond","status":"submitted"}\n\xff\xfe\n' > /tmp/bad.jsonl
  .venv/bin/python -m nine.cli --ledger /tmp/bad.jsonl stats   # UnicodeDecodeError traceback
  touch /tmp/notadir; .venv/bin/python -m nine.cli --ledger /tmp/notadir/x.jsonl stats  # FileExistsError traceback
  ```
  The T4-F1 test only covers JSON-level garbage (`test_corrupt_ledger_line_does_not_brick`), not byte-level corruption.
- impact: a crash-corrupted append (partial binary write) or a `sed`/hand-edit mishap bricks every command including submit/recover — the operator cannot even inspect or recover; the exact "corrupt ledger must not brick" invariant holds for JSON garbage but not for bytes.
- suggested_fix: read with `encoding="utf-8", errors="replace"` (or read bytes and decode per line, counting undecodable lines as corrupt), and wrap mkdir/read/write in the same OSError->LedgerError path (LedgerError already exists and CLI commands print it cleanly). Regression test: ledger with one `\xff` line + two healthy lines → loads, `corrupt_lines == 1`, healthy jobs reachable, submit still works.
- effort: S

## FINDING 3
- area: robustness (corrupt-line skip only validates JSON, not schema)
- severity: medium
- title: Valid-JSON ledger lines with garbage fields crash cancel/transition (`KeyError`) and artifacts (`TypeError`) with raw tracebacks
- evidence: `nine/ledger/ledger.py:158-160` — `_load` applies `Job.__dict__.update` with ZERO field validation, so `"status": "banana"` or `"artifacts": "NOTALIST"` load fine; `:99-106` — `Job.transition` does `LEGAL_TRANSITIONS[self.status]`, so a garbage status raises `KeyError` (not `InvalidTransition`); `nine/cli.py:329-336` (`cmd_cancel`) and `:318-326` (`cmd_artifacts`) catch only `LedgerError`. Repro:
  ```
  printf '{"job_id":"j9","workflow_id":"respond","status":"banana"}\n' > /tmp/l.jsonl
  nine cancel j9   # KeyError: 'banana' traceback (also transition)
  printf '{"job_id":"jA","workflow_id":"respond","status":"shipped","artifacts":"NOTALIST"}\n' > /tmp/l.jsonl
  nine artifacts jA   # TypeError: string indices must be integers (iterates chars)
  ```
  (A future-version status value or any hand-edit produces these; `attempts`/`max_fix_loops` as strings additionally break the FIX-loop comparison with a TypeError.)
- impact: schema-level corruption (valid JSON, wrong types) defeats the corrupt-line skip: the operator gets raw tracebacks instead of the promised clean one-line error, and one bad line makes cancel/artifacts unusable for that job.
- suggested_fix: validate loaded records in `_load` (status in VALID_STATUSES, artifacts/verdicts are lists, attempts/max_fix_loops ints — else count as corrupt); `transition()` should catch KeyError and raise `InvalidTransition`; CLI commands should catch `(LedgerError, KeyError, TypeError)` and print a one-line error. Regression test: garbage-status + string-artifacts lines → `cancel`/`artifacts` exit 1 with clean message, no traceback.
- effort: S-M

## FINDING 4
- area: robustness (secret hygiene — redact() IGNORECASE edge cases: what it MISSES)
- severity: medium
- title: redact() leaves verbatim secrets in the ledger for JSON-quoted credentials, AWS keys, Slack tokens, and `password == x` (partial tail leak)
- evidence: `nine/router/classifier.py:52-64` (5 patterns). Verified verbatim leaks through `redact()` → stored by `JSONLLedger.submit` (ledger.py:180-188) and `RouteDecision.task_redacted` → durable ledger + route events:
  ```
  redact('submit job with {"password": "hunter2"} to the api')      # UNCHANGED (quote blocks pattern 1)
  redact('use {"token": "abc123xyz"} for auth')                     # UNCHANGED
  redact('header {"api_key": "d41d8cd98f00b204e980"} ok')           # UNCHANGED (no sk- prefix)
  redact('aws_secret_access_key=***')  # UNCHANGED ('secret' followed by '_access_key')
  redact('aws_access_key_id=AKIA***')                  # UNCHANGED
  redact('use AKIA*** for the s3 bucket')              # UNCHANGED
  redact('bot token xoxb-***')    # UNCHANGED
  redact('password == hunter2')                                     # 'password=*** hunter2' — VALUE STILL LEAKS
  ```
  Redaction does NOT break valid JSON output (the quote blocks the match, so JSON stays intact — the miss, not breakage, is the bug). README claims "Secret hygiene by design: redaction in logs". T4-F4's test only covers `PASSWORD=hunter2` / `token is sk-...` forms.
- impact: real credentials in real tasks (AWS access/secret keys are the most common dev-task credential; JSON-quoted bodies are ubiquitous in extract/transform tasks) are persisted verbatim in the durable ledger and learn events — precisely the leak T2-F6/T4-F4/T3-F8 were built to stop.
- suggested_fix: add quote-aware patterns (`["']?(password|token|secret|api[_-]?key|aws_secret_access_key|aws_access_key_id)["']?\s*[=:]\s*["']?\S+`), `AKIA[0-9A-Z]{16}`, `xox[baprs]-[A-Za-z0-9-]+`, `ya29\.[A-Za-z0-9_-]+`, and a `==`/`=:` variant that consumes the full value; one pytest per format. 
- effort: S

## FINDING 5
- area: runtime (callable-node timeout — partial-write artifacts)
- severity: medium
- title: Timeout abandons a LIVE writer thread: ghost files land in the job dir after the job is failed, and can contaminate a later recover re-run's manifest
- evidence: `nine/runtime/workflows.py:153-158` — the worker thread is `daemon=True`, joined with `timeout=deadline`; on timeout the thread is ABANDONED (still running with a reference to `job_dir`), the job is transitioned to `failed`, and the caller gets `WorkflowError`. Repro (hermetic):
  ```
  run() { time.sleep(1.2); (job_dir/"GHOST.md").write_text(...) }   # timeout_seconds=1
  ```
  → execute raises at ~1.0s, ledger status `failed` (good), but at ~2.5s `GHOST.md` EXISTS in the job dir and is NOT in the manifest (unregistered ghost). If `nine recover` re-runs while the zombie is still alive, the zombie's late write lands inside the NEW attempt's job dir, where the per-node artifact scan (workflows.py:288-294) registers it as a produced artifact of the new attempt.
- impact: post-timeout partial writes accumulate as unregistered ghost files (possibly containing partial/secret data) in job dirs; on recover they can be misattributed to the new attempt's manifest — the job's artifact record claims content nobody in that attempt executed; the abandoned call (ADK/model/subprocess) also keeps burning quota in the background.
- suggested_fix: make callable-node cancellation real — pass a `threading.Event` that ADK/tool wrappers check, or run callable nodes in a subprocess so the timeout can kill the process tree (mirroring bash `sp.run(timeout=...)`); at minimum track the abandoned thread on the job and refuse `recover` while it is alive, and record a `timeout_leak` note in job.metadata. Regression test: hung node writes after failure → assert the file is neither registered nor present after recover-wipe, or that recover is refused while the zombie lives.
- effort: M

## FINDING 6
- area: robustness (catalog degrade is shape-blind)
- severity: medium
- title: A valid-JSON but wrong-shape catalog.json bricks every `nine` command at import (or crashes routing with AttributeError/TypeError)
- evidence: `nine/registry.py:263-270` — `_merged_keywords` does `for wf, extra in ...: for kw in extra:` with NO type validation; `load_catalog` (`:57-85`) only guards JSONDecodeError/OSError/non-object. Repro (catalog file swapped, restored byte-identical afterwards):
  ```
  {"keyword_overrides": {"build": 5}}     # TypeError: 'int' object is not iterable at import of nine.registry -> EVERY command dies
  {"keyword_overrides": {"build": None}}  # TypeError: 'NoneType' object is not iterable
  {"keyword_overrides": {"build": ["ok", 123]}}  # Router.register: AttributeError 'int' has no attribute 'lower' -> submit bricks; classify: re.escape(123) TypeError
  {"keyword_overrides": {"build": {"a": 1}}}     # silently adds "a" as a keyword (dict iterates keys) -> routing pollution
  ```
  T4-F3's degrade handles broken JSON but not valid-JSON-wrong-shape.
- impact: a bad `nine learn apply` write (or human edit) with valid JSON but wrong types turns the promised "degrade to base keywords" into an import-time brick with a traceback — or, worse, silently pollutes routing with garbage keywords.
- suggested_fix: shape-validate in `_merged_keywords`/`_merged_descriptions`: override values must be `list[str]` (else stderr warning + skip that key, same tone as the JSONDecodeError path). Regression test: int/None/dict/list-with-int overrides → base keywords intact, warning on stderr, no exception.
- effort: S

## FINDING 7
- area: CLI (argparse SUPPRESS fallbacks — surface asymmetry left by T4-F6)
- severity: low
- title: `--workdir` before the subcommand fails with a misleading "invalid choice" error; `--ledger` after the subcommand is rejected for every command except submit/chain
- evidence: `nine/cli.py:587-629` — only `submit`/`chain` re-declare `--ledger`/`--workdir` (SUPPRESS); the parent parser (cli.py:575-578) has `--ledger/--events/--memory` but NO `--workdir`, and `status/discover/artifacts/cancel/recover/stats/memory/learn` do not re-declare `--ledger`. Repro:
  ```
  nine --workdir /tmp/w submit hello   # error: argument cmd: invalid choice: '/tmp/w'  (exit 2, misleading)
  nine status --ledger /tmp/x j1       # error: unrecognized arguments: --ledger /tmp/x
  nine stats --ledger /tmp/x           # same
  ```
  The dangerous silent-clobber (T4-F6) is fixed, but the flag surface is inconsistent: `--ledger` works in BOTH positions only for submit/chain; `--workdir` works in NEITHER position globally.
- impact: scripts that place `--workdir` before the subcommand (natural, given `--ledger` works there) fail with a confusing argparse error; operators cannot put `--ledger` after the verb for status/cancel/recover — asymmetric and undocumented.
- suggested_fix: promote `--workdir` to the parent parser (or re-declare `--ledger/--workdir/--events/--memory` with `default=argparse.SUPPRESS` on every subparser). Regression test: `nine --workdir X submit t` and `nine status --ledger Y j1` both parse and route to the right value.
- effort: S

## FINDING 8
- area: docs / robustness
- severity: low
- title: Exit-code docstring contradicts the code, and `nine memory list` raw-tracebacks on one corrupt memory line (T4-F1 skip-safety bypassed on the CLI read path)
- evidence: `nine/cli.py:16-18` — "Exit codes: 0 ok, 1 error" but `cmd_chain` returns 2 on non-SHIP (cli.py:169), `cmd_submit` returns 2 (cli.py:219, 257) — README's own table ("verdict: SHIP|FIX|BLOCK (exit code ≠ success)") matches the code, the CLI docstring does not. `nine/cli.py:108-110` — `cmd_memory list` re-parses the memory file directly with `_json.loads(line)` and no try/except; repro:
  ```
  printf '{"a":1}\nNOT JSON\n' > /tmp/memory.jsonl
  nine --memory /tmp/memory.jsonl memory list   # json.decoder.JSONDecodeError raw traceback
  ```
  while `MemoryGraph`'s own load skips bad lines (T4-F1). README's "Secret hygiene by design: redaction in logs" overstates redact() coverage (see FINDING 4).
- impact: automation parsing documented exit codes treats SHIP-vs-non-SHIP incorrectly (exit 2 is the verdict signal, not an error); one corrupt/partial memory line bricks the memory CLI even though the store tolerates it.
- suggested_fix: fix the docstring to "0 ok / 1 error / 2 non-SHIP verdict"; route `cmd_memory list` through the store's defensive load or wrap the per-line parse in try/except with a skip+count. Regression test: corrupt memory.jsonl → `nine memory list` prints a clean error and healthy rows.
- effort: S

## New bench fixture proposals (check.sh-style, hermetic, no Gemini)
Note: slots 006/007/008 were filled by the slice-25 commit (25a87c0) mid-session, so these target the next slots (009-011). Each FAILS the naive starter and PASSES a correct candidate; none needs a model.

1. **bugfix-small-009 "no-symlink-evidence"** — candidate implements a small verification helper that must certify ONLY real files. check.sh: builds a workspace, creates a decoy `EVAL.json` OUTSIDE it, `ln -s` it inside, runs the candidate, and asserts the candidate (a) refuses to treat the symlink as evidence, (b) never registers a file whose resolved path escapes the workspace. Would have caught FINDING 1 (symlink evidence poisoning) as a regression test for the debug/build lane.
2. **bugfix-small-010 "non-utf8-ledger-tolerance"** — candidate implements `load_records(path)` that must survive a file containing invalid UTF-8 bytes, truncated JSON lines, and healthy lines: skip bad records, return the healthy ones, report a damage count, never raise. Starter: naive `open().read().splitlines()` + `json.loads` → crashes on `\xff`. Would have caught FINDING 2 (+3) — byte-level and schema-level corruption — exactly the "one corrupt line must not brick" invariant.
3. **bugfix-small-011 "redact-all-credential-shapes"** — candidate implements `redact(text)`; check.sh asserts none of the seeded secrets survive in the output across 10 shapes: `KEY=value`, `KEY: value`, `KEY is value`, `KEY == value`, JSON `"key": "value"`, `aws_secret_access_key=...`, `AKIA...`, `xoxb-...`, `Bearer ...`, PEM block. Starter: single `(?i)(password|token|api_key)=...` regex → fails JSON/AWS/`==` cases. Would have caught FINDING 4 and would have made the T4-F4/T3-F8 redaction tests format-complete.

Severity count: 2 high / 4 medium / 2 low (8 findings). All repros ran against HEAD 25a87c0; working tree left clean.
