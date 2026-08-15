# Torture-23 Gap Report (round 12) — workflows + router + CLI + docs, slice-43 edges

Worker: torture-23 (TORTURE-TESTER-23). Hermetic only (zero Gemini quota).
Surface: gate-timeout machinery (NINE_GATE_TIMEOUT_S), CLI OSError belt,
NINE_NODE_TIMEOUT_S pre-validation, best-effort aux writes, redact() families,
server 422/400 semantics, router, plugin registry, truncation, bash quoting,
README claims, learn/apply-revert, recover/cancel paths.

## FINDING 1
- area: gates
- severity: medium
- title: gate crash (BaseException) is indistinguishable from a hang: full timeout wasted, verdict summary LIES ("FIFO/device?"), exception swallowed, daemon thread leaked
- evidence: nine/runtime/workflows.py:284-302 (_run_gate thread) — the worker thread does `q.put(self.gate.evaluate(...))` with NO try/except; EvidenceGate.evaluate only catches `except Exception` (nine/gates/evidence.py:67), so a check raising SystemExit/KeyboardInterrupt (BaseException) kills the thread silently. Main thread then waits the FULL NINE_GATE_TIMEOUT_S and returns BLOCK with summary "gate timed out after Ns (evidence read hung - FIFO/device?)" and empty eval_results — the crash is invisible and mislabeled. Repro (/tmp/t23_repro1.py): check raises SystemExit(1) -> elapsed 1.01s (full window), verdict BLOCK, summary "evidence read hung - FIFO/device?", eval_results {}. Thread leak: on timeout the abandoned thread (still blocked in a FIFO read, or later blocked forever on q.put into the full maxsize=1 queue) is never joined/cleaned — a long-running server leaks one thread + open fd per gate timeout.
- impact: (a) a crashed plugin/workflow gate costs the operator the full 60s gate window and a BLOCK whose summary points at the WRONG cause (evidence hang); (b) no traceback/diagnostics anywhere, so the real bug is invisible; (c) repeated gate timeouts on the server leak daemon threads + fds indefinitely.
- suggested_fix: wrap the worker in try/except BaseException: on exception, put a record with verdict BLOCK (or FIX), summary f"gate check crashed: {exc!r}" and log the traceback to stderr; keep the timeout for genuine hangs only. Regression test: check raising SystemExit with NINE_GATE_TIMEOUT_S=30 must return BLOCK in <2s with "crashed"/"error" in the summary, not "timed out".
- effort: S

## FINDING 2
- area: CLI
- severity: high
- title: `nine recover` wipe-loop is outside the OSError belt: a PermissionError during the stale-artifact wipe raw-tracebacks AFTER ledger.recover() already stamped the job `recovered` — durable tombstone, second recover refuses
- evidence: nine/cli.py:632-643 — `ledger.recover()` (nine/ledger/ledger.py:379-382) transitions blocked/failed -> `recovered` and appends the durable line FIRST; the artifact wipe loop (638-643, `p.unlink()` / `shutil.rmtree(p)`) has NO try/except and is not covered by the T22-F3 belt (that belt wraps only `_execute_job`, cli.py:350). Repro (/tmp/t23_repro2.py): job failed, job_dir contains a chmod-555 subdir -> `nine recover` prints a RAW PermissionError traceback; durable status is now `recovered`; a second `nine recover` cleanly refuses ("only blocked/failed can be recovered") — job dead-ends, only hand-editing the ledger or cancel+resubmit (losing the task context) salvages it.
- impact: any unremovable artifact (read-only dir, EBUSY, cross-user file) turns recover into a tombstone with a raw traceback — exactly the zombie class T22-F3 was meant to eliminate, one call-site short. Operator loses the recover path AND the raw-task context (task.txt lives in the wiped dir).
- suggested_fix: wrap the wipe in try/except OSError -> one clean line + transition the job back to blocked (or failed) with a best-effort ledger.update so a second recover is legal after the operator fixes permissions; never leave the job at `recovered` without executing. Regression test: failed job + read-only artifact subdir -> cmd_recover returns 1, durable status back to blocked/failed, no traceback, second recover succeeds after chmod.
- effort: S

## FINDING 3
- area: CLI
- severity: low
- title: `nine recover` raw-tracebacks UnicodeDecodeError on a corrupt (invalid UTF-8) task.txt — the pre-wipe task read has no error handling (only the missing-file case is handled)
- evidence: nine/cli.py:599-601 — `if task_txt.exists(): task = task_txt.read_text(encoding="utf-8")` — a task.txt with invalid UTF-8 raises UnicodeDecodeError (a ValueError) with no try/except anywhere in cmd_recover (the except at cli.py:648 wraps only _execute_job); cli.py main has no catch-all. Repro: failed job + task.txt containing b"\xff\xfe" -> `nine recover` prints a raw `UnicodeDecodeError 'utf-8' codec can't decode byte 0xff` traceback. No state damage (fails before ledger.recover()), but the operator gets a Python traceback instead of the documented one-clean-line error contract, and the hint "task.txt is missing" path (which does print cleanly) never fires for the corrupt case.
- impact: an operator whose task.txt was truncated/garbled by a crash sees a raw codec traceback and must guess the cause; violates the T22-F3/T4 clean-error contract on a sibling read path.
- suggested_fix: wrap the read in try/except (UnicodeDecodeError, OSError) -> same clean error as the missing-file branch ("task.txt unreadable/corrupt (raw task not available) — restore the workdir or re-submit"). Regression test: invalid-UTF-8 task.txt -> cmd_recover returns 1 with a single clean line.
- effort: S
