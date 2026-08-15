# torture-26 — runtime + gates attack surface (adversarial)

**Round:** 26
**Focus:** adk_runtime retry/empty-stream/RunConfig · EVAL gate parsing (self-test/verify/evidence gate) · ledger JSONL↔Firestore parity · redaction · recover/cancel
**Date:** 2026-08-15 · **Tester:** TORTURE-TESTER-26
**Budget:** hermetic only (zero Gemini quota — no submissions; imports + in-repo code-path analysis + /tmp micro-repros).

## Findings (2)

### F1 — MEDIUM — stale-guard input-hash read is the last UNBOUNDED read in the runtime: a FIFO at a gate-certified SEEDED input hangs the executor forever (cancel-proof, outside NINE_GATE_TIMEOUT_S)

- File: `nine/runtime/workflows.py`, stale-artifact guard, the `expected_name in inputs_ok` branch (~line 902-905):
  `unchanged = self._hash(p_expected.read_bytes()) == snap.get(expected_name)` — runs on the MAIN thread inside `execute()`, with only `except OSError`. A FIFO at that path makes `read()` block forever (no OSError). Every sibling read in the same guard has an `is_file()` guard — the symlink branch (820), the dir-member branch (874-879), and the T17-F1 re-hash loop (956 `mp.is_file()`) — but THIS branch does not. T21-F1 (slice-43) bounded `gate.evaluate()` in a daemon thread + `NINE_GATE_TIMEOUT_S`; the stale guard runs AFTER that thread returns, on the main thread, with no deadline.
- Reachability (no operator needed): `nine/workflows/test_wf.py` gate = `eval_json_check()` + `exit_codes_check()` + `required_artifact_check(["test_solution.py", "EVAL.json"])`, and `test_solution.py` is a SEEDED attempt-1 input in bench (`inputs_ok` = attempt-1 snapshot). A torture-style task ("write a test suite whose collection replaces itself with a named pipe") makes the model-written `test_solution.py` `os.mkfifo("test_solution.py")` mid-run; pytest already has the file open, completes, EVAL.json passes. `required_artifact_check` PASSES because `exists()` is True for a FIFO (T24-F3 only guards the ADK write side; T24-F1 only guards the verify bash node + load_eval_json; the FIX-loop entry vector `mkfifo` over a seeded file via a bash node or operator also applies, T21-F1's acknowledged vector).
- Verified (hermetic, in /tmp): (a) with EVAL.json valid, `required_artifact_check(["test_solution.py","EVAL.json"])` → **PASS** and `eval_json_check()` → **PASS** with a FIFO at test_solution.py (all SHIP conditions met); (b) the exact `p_expected.read_bytes()` pattern on that FIFO **blocked past a 3s timeout** (exit 124, would be forever).
- Impact: verdict SHIP → stale guard reads the FIFO → executor wedged indefinitely — no node timeout (nodes already done), no gate timeout (thread returned), `nine cancel` ineffective (the `_cancelled` poll at line 994 is AFTER the stale guard; the main thread never gets there). Operator must kill the process group; a wedged job_dir then needs manual cleanup.
- Fix suggestion: `is_file()` (or `stat.S_ISREG`) guard before `read_bytes()` in the inputs_ok branch — non-regular → treat as modified (unchanged=False → BLOCK), mirroring the T24-F1/T24-F4 `_regular()` discipline. Add a regression: FIX-loop attempt with FIFO at a seeded expected input → fast BLOCK, no hang, cancel still works.

### F2 — LOW — gate "non-empty artifact" checks accept a DIRECTORY as a non-trivial answer (doc/consistency: docstring says `file`, implementation checks only exists+size)

- Files: `nine/gates/evidence.py` `file_nonempty_check()` (~line 115) and `required_artifact_check()` (~line 196): `file_nonempty_check` guards `is_symlink or not exists()` then `f.stat().st_size` — a directory at `RESPONSE.md` is NOT a symlink, exists, `st_size` of a fresh dir is 96B ≥ min_chars → **PASS** ("RESPONSE.md present and non-trivial (96B)"). `required_artifact_check` likewise passes (`exists()` True). Contrast the is_file() discipline already applied to every sibling read: load_eval_json (T21-F1), verify `_regular` + `_safe_out` (T24-F1), `contained_write` write guard (T24-F3), recover task.txt (T23-F3).
- Reachability: `nine/runtime/responder.py` registers BOTH checks for RESPONSE.md; `contained_write` happily creates the directory (`target.parent.mkdir(parents=True)`) when the model writes `RESPONSE.md/notes.txt` instead of `RESPONSE.md` (a plausible model slip/misname) — gate then SHIPs a FOLDER as the "answer". `summarizer.py` `src.read_text` on a directory source raises IsADirectoryError (loud node FAIL, bounded — no hang).
- Verified hermetic: `file_nonempty_check("RESPONSE.md", min_chars=10)({}, d)` → `True` with RESPONSE.md as a directory (96B).
- Impact: gate certifies a directory as non-trivial text evidence; violates the documented "artifact file" contract (docstring at line ~115: "requires an artifact file exists and is not empty") and the runtime's own established is_file() doctrine. No leak/hang — LOW.
- Fix suggestion: add `or not f.is_file()` to the missing-condition in `file_nonempty_check` (and `required_artifact_check`) so non-regular paths fail fast like every other family member.

## Rejected (already fixed in LEDGER — checked, NOT re-filed)

- EVAL.json strict-boolean contract (S24-F1); verify-stub SHIP + fabricated Verdict: PASS (T3-F1/F2); corrupt-ledger-line skip+count (T4-F1); recover-unknown-id clean error (T2-F7); recover on running/shipped jobs (T3-F3); node timeouts via daemon thread (T3-F5); redaction incl. quoted creds/AKIA/xoxb (T6-F4); ghost-file contamination (T6-F5 partial, recover wipes); symlink manifest hole (T8-F1); NINE_MAX_LLM_CALLS junk-env warning (T24-F5); stale-artifact `.expected` provenance (T17-F2/T20-F5); pid pruning (T15-F9); gate hang timeout thread (T21-F1); gate-crash BLOCK (T23-F2); FIFO reads/writes guarded in verify node + contained_write + load_eval_json + review gate (T24-F1/F3/F4); Firestore submit/update validation + recover contract (T18-F1, T19-F6, T24-F2); CANCELLED verdict durable (T18-F3).

## Notes on other focus areas (no findings)

- adk_runtime.py 173-290: 3-attempt empty-stream retry with backoff, fresh session per attempt, `LlmCallsLimitExceededError` fails loud instead of burning retries, non-empty events short-circuit to success — no silent empty-pass remains.
- JSONL↔Firestore parity: `_job_from_rec` shape guard, `recover` raises on non-blocked/failed, `update` validates full record — parity holds.
