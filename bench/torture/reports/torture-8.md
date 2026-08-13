# TORTURE-TESTER-8 Report — attack surface: runtime deep edges + fixtures error paths

Worker: TORTURE-TESTER-8 (round 5: node timeout/retry semantics, cancel control-plane,
concurrency, symlink containment leftovers, learn-store byte tolerance, fixture proposals)
Repo HEAD: 28d4a85 (slice 25 — 287 passed, 5 skipped at start of session).
All repros hermetic (no Gemini, no network): `.venv/bin/python -c` snippets in /tmp, real
fixtures/ledger files never touched. No git operations performed.

Re-attacked surfaces that HOLD (not re-filed): ledger + memory `search_context` byte tolerance
(T6-F2), garbage-status ledger schema guard (T6-F3), recover refuses non-blocked/failed cleanly
(T3-F3/T4-F2), recover refuses missing task.txt (T5-F4), gate checks treat symlinks as missing
(evidence.py:83/102/167), whitespace-key guards, EVAL strict-boolean gate (slice-24). The
findings below are NEW angles: symlink containment gaps on the *manifest/delete* sides that the
T6-F1/T5-F1 sweep did not cover, timeout/retry/cancel control-plane lies, and byte-level
corruption in the learn stores that T4-F1/T6-F2 claims do not actually cover.

---

## FINDING 1
- area: runtime
- severity: high
- title: Artifact manifest STILL registers symlinked outside content as job evidence — T6-F1 hardened the gate but never the registration loop (its own test passes with a dangling symlink)
- evidence: `nine/runtime/workflows.py:306-315` — the manifest loop does `if not p.is_file(): continue; st = p.stat(); data = p.read_bytes()`; all three FOLLOW symlinks, so a symlink to a REAL file is registered with the outside file's sha256/size and `produced_by` = the node (`:340-341` explicit-artifact branch does the same). Repro (hermetic, SHIPs):
  ```
  bash node: echo 'hello world response' > RESPONSE.md; ln -sf /abs/outside/EVAL.json EVAL.json
  ```
  → verdict SHIP, manifest contains `EVAL.json  72f197f222e9  59B  by respond` where `72f197f222e9` is the sha256 of the OUTSIDE file. The T6-F1 regression test `tests/test_torture_harvest_4.py:214-249` passes only because its `ln -sf ../outside/EVAL.json` target is DANGLING (`../outside` from the job cwd resolves to `work/outside/EVAL.json`, which never exists) — so `is_file()` is False and the "registration skips symlinks" claim is never exercised with a real target.
- impact: the manifest (and chain rollup `chain.py:232-234` + memory graph `_save_memory`) certifies content that was never produced in the workspace — cross-job evidence poisoning on the READ/registration side, exactly the lie T6-F1 claimed to kill. Any job whose gate does not itself require EVAL.json (e.g. `respond`) SHIPs with a symlinked outside file recorded as its own evidence.
- suggested_fix: in the registration loop and the explicit-artifact branch, treat symlinks as non-evidence: `if p.is_symlink(): continue` (and read via `os.open(..., O_NOFOLLOW)` if a read is ever needed). Regression test: seed job dir with a symlink to a REAL outside file → assert the manifest excludes it AND (fix the existing test's dangling target so it actually tests a resolvable symlink).
- effort: S

## FINDING 2
- area: CLI
- severity: high
- title: `nine recover` wipes files THROUGH a symlinked job_dir — the containment sweep covered model WRITES but the operator DELETE path deletes arbitrary directories
- evidence: `nine/cli.py:387-391` — `for p in job_dir.iterdir(): if p.is_file() or p.is_symlink(): p.unlink(); elif p.is_dir(): shutil.rmtree(p)` — `iterdir`/`unlink`/`rmtree` all follow the job_dir symlink. Repro (hermetic):
  ```
  # a model-driven bash node (cwd=job_dir) does: cd /tmp; rm -rf <job_dir>; ln -s <victim> <job_dir>
  job_dir.symlink_to(victim); victim/important.txt + victim/sub/nested.txt exist
  # cmd_recover's wipe loop -> victim emptied: important.txt DELETED, sub/ rmtree'd
  ```
  The job_dir must contain a readable `task.txt` for recover to proceed — the attacker writes it through the symlink first (recover's task check `cli.py:366-371` follows the symlink too).
- impact: a confused/prompt-injected model can turn the operator's routine `nine recover` into an arbitrary-directory deleter (and `_execute_job` then writes task.txt/artifacts into the victim dir). T3-F7/T5-F1/T6-F1 contained model-controlled WRITES; the recover wipe is an unguarded destructive path of the same family.
- suggested_fix: in `cmd_recover`, refuse loudly when `job_dir.is_symlink()` (before any wipe) — a job whose workspace is a symlink is already compromised; wipe with `os.scandir(job_dir)` + `entry.is_symlink()` checks and `resolve()`-verified containment. Regression test: `work/<job_id>` → symlink to a victim dir with files → `nine recover` refuses, victim untouched, job stays blocked/failed.
- effort: S

## FINDING 3
- area: workflows
- severity: high
- title: `nine cancel` during a running attempt is a no-op — cross-process the job SHIPs anyway (last-line-wins undo), in-process the executor dies with a raw InvalidTransition traceback, and a chain-container cancel is ignored via force_terminal's direct-set
- evidence: `nine/ledger/ledger.py:211` (`_load` last-line-wins), `nine/runtime/workflows.py` execute() transitions an in-memory job copy and never re-reads the ledger; `ledger.py:102` raises InvalidTransition for cancelled→awaiting_evidence; `nine/chains/chain.py:84-85` `force_terminal` fallback DIRECT-SETS `job.status` bypassing legal transitions. Repro A (cross-process, real CLI shape): executor (ledger instance A) running a 1.2s tool node; process B (`ledgerB.cancel`) appends `cancelled`; A finishes SHIP and appends `shipped` → fresh load shows **shipped** — the operator's cancel was silently undone. Repro B (in-process): cancel mid-run → `InvalidTransition('illegal transition cancelled -> awaiting_evidence')` escapes `execute()` (cli.py:238-243 catches only WorkflowError) → raw traceback. Repro C (chain): cancel the container job mid-chain → hops keep running → chain final SHIPPED, container status `shipped` (10 hop jobs still executed).
- impact: cancel is a lie. A runaway job the operator explicitly cancelled still SHIPs verified artifacts and stamps the ledger shipped; automation cannot trust cancel; in-process it crashes the submit CLI instead.
- suggested_fix: cooperative cancellation end-to-end: pass a `threading.Event` cancellation token into `WorkflowExecutor`/`ChainExecutor` (see FINDING 5 design — same token), and have the executor (a) re-check the ledger status (or the Event) between nodes, between FIX attempts, and before every terminal transition, aborting with status `cancelled` when set; (b) make `cancel` set the token for live runs (server-side registry) and refuse with a clean message when the job is not in a cancellable state. Regression test: cancel mid-run (cross-process ledger) → final ledger status is `cancelled`, no `shipped` line appended, no artifact rollup.
- effort: M

## FINDING 4
- area: workflows
- severity: high
- title: Callable-node timeouts are NEVER retried — the timeout WorkflowError is classified "deterministic" while `_run_node`'s own docstring promises retries on timeout; bash timeouts ARE retried (asymmetric, and max_retries is dead code for callable nodes)
- evidence: `nine/runtime/workflows.py:155-163` raises `WorkflowError("node ... exceeded timeout ...")` on callable timeout; `:191-194` `except WorkflowError: raise  # Deterministic failure ... retrying cannot fix it`; docstring `:172-173` claims "Retries on any raised exception (timeout, Gemini 429/503, flaky tool)". Repro: tool node `timeout_seconds=0.05, max_retries=3` sleeping 0.3s → invoked exactly **1×**, job `failed` (a bash node with the same timeout raises `TimeoutExpired`, a plain Exception, and IS retried). Related config trap (same area): `timeout_seconds=0` (and negative) makes EVERY node fail instantly — bash `sp.run(timeout=0)` raises TimeoutExpired even for `echo done > out.txt`, and callable `worker.join(timeout=0)` always finds the thread alive → "exceeded timeout 0s"; `timeout_seconds=None` works (wait forever) but is undocumented, and there is NO node schema validating `timeout_seconds >= 1` (schemas/ has no node.schema.json).
- impact: a single transient stall (free-tier hang, slow tool) kills the job in one attempt even with max_retries=3 — the entire retry/backoff machinery (max_retries, retry_delay_seconds, jitter) is dead code for callable nodes; a user setting 0 to mean "no timeout" gets guaranteed failure.
- suggested_fix: raise a distinct retryable error (e.g. `NodeTimeout`) for callable timeouts and let `_run_node` retry it per max_retries (mirroring bash TimeoutExpired); validate `timeout_seconds >= 1` at `Node.__post_init__` (reject 0/negative loudly) or document 0 = no-timeout explicitly. Regression test: timeout node with max_retries=3 invoked 4× and succeeding on the last attempt; `timeout_seconds=0` either means no-timeout or fails at construction.
- effort: S

## FINDING 5
- area: runtime
- severity: high
- title: bash-node timeout SIGKILLs only the shell — orphaned grandchildren keep running and write ghost files into the job dir after the job failed (process twin of T6-F5; the cooperative-cancellation design belongs here too)
- evidence: `nine/runtime/workflows.py:126-130` — `sp.run(node.command, shell=True, ..., timeout=node.timeout_seconds)`; on timeout subprocess.run SIGKILLs only the direct `/bin/sh` (no `start_new_session`/process group), so grandchildren survive. Repro (hermetic): command `(sleep 1.2; echo GHOST > GHOST.txt) & sleep 60`, `timeout_seconds=0.4` → node fails at 0.4s, but `GHOST.txt` appeared at ~1.2s (orphan wrote AFTER the job failed) and the `sleep 60` process leaked. This is the same abandoned-writer class as T6-F5 (which covered daemon THREADS and records `timeout_abandoned_worker` metadata); nothing records or kills bash orphans, and the ghost file can land after a `recover` wipe has already started (TOCTOU, contaminating the re-run's manifest).
- impact: timed-out bash nodes leak processes and can drop unregistered ghost files into the job dir that a recover re-execution then registers as its own evidence; on real workloads this is a model-invoked `nohup server &` or test daemon left behind.
- suggested_fix: (1) start the shell in its own process group (`preexec_fn=os.setsid` / `start_new_session=True`) and on timeout SIGTERM the GROUP, then SIGKILL after a grace period — record `timeout_abandoned_processes` in metadata like T6-F5. (2) COOPERATIVE CANCELLATION (T6-F5 partial, this round's ask): pass a `threading.Event` into callable nodes as a third param with default None (`run(inputs, job_dir, cancel_event=None)`); the timeout path sets it, and cooperative callables (ADKAgentNode's retry loop + before writing agent_output.md, gemma_generate/summarizer before writing TEACH.md/HANDOFF.md) check it and stop writing. API-safety proof: all 40 callable `run` functions across nine/ share the uniform `(inputs, job_dir)` signature (grep `def _run(inputs` = 40 sites; responder/flagship included), so a defaulted third parameter is backward-compatible by construction — non-cooperating callables behave exactly as today, cooperating ones stop writing on cancel. Regression test: bash node spawning a background writer with a short timeout → after failure the grandchild PID is gone and no ghost file appears; callable node with a cooperating run that checks the Event → no file written after the timeout fires.
- effort: M

## FINDING 6
- area: robustness
- severity: medium
- title: A job left at `running` by a crash/power-loss is UNRECOVERABLE — recover refuses (blocked/failed only), cancel tombstones it, and no --force/stale-run path exists
- evidence: `nine/ledger/ledger.py:270-283` — `recover()` raises `LedgerError("job ... is running, only blocked/failed can be recovered")`; `LEGAL_TRANSITIONS` (`:77-97`) offers no running→failed/blocked operator path; `nine/cli.py:352-381` `cmd_recover` has no force flag. Repro (hermetic): ledger line `"status":"running"` → `recover` refused; `cancel` ok (running→cancelled) → `recover` refused again (cancelled); job permanently stuck at `cancelled`; `discover --status running` shows it forever with no way forward.
- impact: after a crash (SIGKILL, power loss, deploy), the operator cannot resume the job — must re-submit with a NEW job_id, losing identity, artifact lineage, and ledger continuity; with quota cooldowns a crashed run wastes the entire budget with no path forward.
- suggested_fix: add `nine recover --force` (or a stale-running sweep: `running` jobs with `updated_at` older than N minutes auto-degrade to `failed`, then recoverable) with a loud warning; reset attempts like the normal recover path. Regression test: running job → `recover --force` re-executes and transitions recovered→running→shipped.
- effort: S

## FINDING 7
- area: robustness
- severity: medium
- title: One non-UTF8 byte in events.jsonl/candidates.jsonl bricks `nine learn events|candidates|apply|revert` with a raw UnicodeDecodeError; `LocalMemoryGraph.__len__` likewise — T4-F1's "learn stores too" claim and T6-F2's byte fix never reached these stores
- evidence: `nine/learn/learner.py:82` (`RouteEventStore.all`: `self.path.read_text()`), `:121` (`CandidateStore.all`), `:146` (`update_status`) and `nine/memory/graph.py:122` (`__len__`: plain `open(..., encoding="utf-8")`) — none use `errors="replace"`, so the per-line json try/except never runs (decode fails first). CLI repro (hermetic):
  ```
  printf '{"event_id":"ev-1",...}
ÿþ
' > events.jsonl
  .venv/bin/python -m nine.cli --events /tmp/events.jsonl learn events
  → UnicodeDecodeError traceback, exit 1
  ```
  The T6-F2 test (`tests/test_torture_harvest_4.py:254-275`) covers only the ledger; no test covers byte corruption in the learn stores.
- impact: the exact corruption class already fixed for the ledger and memory search still bricks the entire LEARN surface — and `nine learn apply/revert` is the ONLY router-change path, so a crash-corrupted append makes the system's self-improvement loop inoperable.
- suggested_fix: read with `encoding="utf-8", errors="replace"` (or per-line byte decode) in all four sites, skipping+counting bad lines like the ledger. Regression test: events + candidates files with one `ÿ` line → `all()` returns healthy records, `update_status` still rewrites cleanly.
- effort: S

## FINDING 8
- area: docs
- severity: low
- title: Stale "Gemini 3.5 Flash" claims survive the slice-22 doc-truth sweep in code docstrings + docs; code defaults gemini-3.6-flash everywhere
- evidence: `nine/runtime/gemma.py:3` ("nine's primary model is Gemini 3.5 Flash (mandatory)"), `nine/chains/flagship.py:199` ("Gemini 3.5 Flash via google-adk"), `nine/router/classifier.py:135` ("Model router using Gemini 3.5 Flash"), `docs/architecture.svg:41,71,141`, `docs/demo-script.md:12,22`, `docs/ADAM-RUNBOOK.md:24` — while `nine/cli.py:39` (`_routing_model` uses `gemini-3.6-flash`), `nine/runtime/responder.py:17` and README.md:181 ("Gemini 3.5 or newer | Gemini 3.6 Flash") are correct. Related: `Node.timeout_seconds` (workflows.py:46) has no schema and the 0/None semantics (FINDING 4) are undocumented.
- impact: docs lie about the model actually serving jobs — the exact doc-truth class slice-22 set out to kill; operators can't learn timeout semantics from any spec.
- suggested_fix: sweep the three docstrings + docs to `gemini-3.6-flash`; document timeout_seconds semantics (>=1, 0 rejected/None=wait-forever) in the Node docstring. Regression: extend the existing README doc-truth test with a grep for `3.5 Flash` in nine/ + docs/.
- effort: S

---

# FIXTURE PROPOSALS — bugfix-small-009 / 010 / 011

Reserved slots confirmed free (T4-F7 took 006-008; bench_nine.py default range is 1..8). All three
reuse the proven 006-008 pattern (starter-broken negative control + fixed-candidate positive,
`tests/check.sh` with embedded PYEOF runner + `test(...)`/`test_raises(...)` calls so
bench_nine's check.sh→pytest conversion works, rubric.json dimensions with weights).

## FIXTURE 009 — bugfix-small-009 (retry/backoff edge cases: max_retries=0, negative)
- task: implement `run_with_retry(fn, *, max_retries=0, base_delay=0.01, jitter=0.1)` for an agent
  runtime. Contract: call `fn()`; on success return `(result, attempts)`. On `TransientError`
  retry up to `max_retries` additional times (total attempts = max_retries+1), sleeping
  `base_delay * 2**(attempt-1)` scaled by `1 + uniform(-jitter, jitter)` between attempts. On
  `ValueError` (deterministic) raise immediately with ZERO retries. `max_retries=0` or negative:
  exactly ONE attempt, NO sleep ever. After the last retry fails, raise `RetryExhausted` carrying
  the last error.
- starter bug: catches `Exception` (retries deterministic failures too), sleeps even when
  `max_retries=0`, and the loop is off-by-one (runs `max_retries+2` attempts).
- fixed behavior: only `TransientError` retried; `max_retries=0`/`-1` → single attempt, no sleep;
  exact attempt counts (use a wrapper that counts invocations); jitter stays within `[-j, +j]`.
- rubric dimensions: `retry_count` (0.4, exact attempts incl. 0/negative), `deterministic_no_retry`
  (0.3, ValueError raised with 1 invocation), `no_sleep_zero` (0.2, max_retries=0 must not sleep),
  `style` (0.1, stdlib only, signature unchanged).
- check.sh shape: PYEOF runner counting invocations via a closure wrapper; test cases for
  transient-then-success (asserts attempts == max_retries+1), always-transient (RetryExhausted,
  attempts == max_retries+1), ValueError (1 invocation, ValueError propagates), max_retries=0/-1
  (1 invocation, no sleep — measure via a short deadline), jitter bounds. Expected tests ~8.

## FIXTURE 010 — bugfix-small-010 (cooperative cancellation semantics: cancel twice, cancel before run, no work after cancel)
- task: implement `CancellableWorker` used by an agent runtime to stop a long job cooperatively.
  Contract: `cancel()` sets the flag; `run(steps)` executes steps in order, checking the flag
  BEFORE each step; when cancelled mid-way it returns `("cancelled", n)` where n = steps completed
  and performs NO further work (and raises nothing); `cancel()` is idempotent (calling it twice is
  safe); `cancel()` before `run()` → `run` returns `("cancelled", 0)` immediately; all steps done
  → `("completed", len(steps))`; must be thread-safe (use `threading.Event`).
- starter bug: `run` ignores the flag (always completes all steps), raises `RuntimeError` on the
  second `cancel()`, and `cancel()`-before-`run()` leaves a step executing.
- fixed behavior: every step is gated on the flag; cancel is idempotent and immediate; a cancelled
  worker never executes a step after the flag is set, never raises; thread-safe via Event.
- rubric dimensions: `cancel_mid_run` (0.4, exact partial count, no post-cancel writes), `idempotent`
  (0.2, double cancel + cancel-before-run), `thread_safety` (0.2, concurrent cancel during run with
  a slow step never lets a step start after the flag), `style` (0.1, threading.Event, stdlib only).
- check.sh shape: PYEOF runner with a step function that appends to a shared list and sleeps;
  `test_raises` cases for the starter's double-cancel crash; a concurrency case using two threads
  (cancel thread + run thread). Expected tests ~7.

## FIXTURE 011 — bugfix-small-011 (atomic JSONL append + corrupt-line/non-UTF8 tolerance under concurrent writers)
- task: implement `append_record(path, record)` and `load_records(path)` for a JSONL store used as
  an append-only audit log. Contract: `append_record` must never interleave or lose a line when two
  threads/processes append concurrently (each line written as ONE atomic write); `load_records`
  must return all healthy records and NEVER raise on one corrupt/partial line or a non-UTF8 byte —
  it must skip the bad line (and count it) and keep the healthy ones.
- starter bug: `append_record` does read-all → append → write-all (lost update under concurrency)
  or writes in chunks (mid-line interleaving); `load_records` does `json.loads` on every line with
  no try/except and `read_text()` with no `errors="replace"` (crashes on a `ÿ` byte).
- fixed behavior: append via a single `f.write(json.dumps(record) + "
")` on a file opened `"a"`
  (O_APPEND — POSIX-atomic per write) with no read-modify-write; load reads with
  `errors="replace"` (or per-line decode) and skips+counts unparsable lines.
- rubric dimensions: `concurrent_no_loss` (0.35, 8 threads × 50 appends → all 400 records loadable,
  no interleaved lines), `corrupt_skip` (0.35, one garbage line + one `ÿ` line → skipped+counted,
  healthy records intact, no raise), `atomicity` (0.2, every loaded line parses as exactly one JSON
  object), `style` (0.1, stdlib only).
- check.sh shape: PYEOF runner with a threading concurrency section (stdlib `threading`), a
  byte-level corruption case written via `open(..., "wb")`, and test_raises for the starter's
  UnicodeDecodeError. Expected tests ~7.

All three stress behaviors this round's probes found UNTESTED in nine: retry/backoff edge cases
(FINDING 4), cancel semantics (FINDING 3 + T6-F5 cooperative design), and atomic-append +
corrupt-byte tolerance (FINDING 7). Each maps 1:1 to a nine invariant that currently has no
regression test.
