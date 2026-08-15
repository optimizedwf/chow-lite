# Robustness Audit — env-var / corrupt-JSONL / missing-files / permission / CLI-exit findings

Worker: robustness-audit (child of chow manager session). Repo: /Users/adam26/chow-work/chow-lite.
Method: hermetic repros only — temp NINE_DATA_DIR, in-process registry injection, real modules, `.venv/bin/python`
scripts under /tmp. No repo files modified; no Gemini/network calls. Full-suite noise excluded:
test_adk_runtime_armor::test_session_created_fresh_per_attempt + test_probe::test_demo_probe_smoke are flaky
(pass standalone); test_t11_f2/f3 + test_wf collection-error target nine/workflows/debug_wf.py (mid-edit, out of scope).

3 confirmed findings (2 high, 1 medium), each with a hermetic repro.

---

## FINDING 1 — high — Auxiliary-store writes are unwrapped on every surface: a SHIPPED job can end in a raw traceback (CLI) or HTTP 500 (server); a client retry then duplicates it
area: learn/memory/catalog write paths (LEARN loop stores + router catalog)
evidence:
- nine/learn/learner.py:75-78 `RouteEventStore.record` -> `self.path.open("a")` + `f.write(...)` — bare, no OSError wrap
- nine/learn/learner.py:115-117 `CandidateStore.append`, learner.py:142-160 `update_status` (full-file `write_text`) — bare
- nine/memory/graph.py:92 `save_artifact_summary` -> `open(self.path, "a", encoding="utf-8")` — bare
- nine/registry.py:91-92 `save_catalog` -> `_CATALOG_PATH.write_text(...)` — bare (called unwrapped at cli.py:754/817/822)
- nine/cli.py:319-320 — post-SHIP `_record_route_event(learner, job, decision, result["verdict"])` runs AFTER the shipped
  verdict, inside no try; cmd_submit's only catch (cli.py:368) is `(WorkflowError, ValueError)` -> OSError = raw traceback
- nine/cli.py:313-316 — same call inside `except WorkflowError:` — an OSError there replaces the clean one-liner with a traceback
- deploy/server.py:513 — post-SHIP `_record_route_event` unwrapped; server global handlers are only
  WorkflowError->502 (122-126), LedgerUnavailable->502 (129-134), ChainError->502 (137-141) -> OSError = FastAPI default HTTP 500
- deploy/server.py:503-506 — same inside `except WorkflowError:` (re-raised handler only covers WorkflowError)
- nine/chains/chain.py:239-258 `self.learner.observe(...)` per shipped hop + chain.py:296-297 `self._save_memory(...)`
  -> memory/graph.py:92 — both unwrapped; chain.py:139-147 execute() catch-all re-raises raw (marks container job failed)
- Construction-time wraps DO exist (learner.py:73 `self.path.touch()` is wrapped by _learner/get_learner); only the WRITE path is raw
hermetic repro (/tmp/nine_findings_repro.py, Case A): probe workflow SHIPs deterministically; a bash node deletes
events.jsonl and mkdirs it as a directory mid-run. Result: ledger shows 7 lines, last status `shipped`, yet
`cmd_submit` RAISED `IsADirectoryError: [Errno 21] Is a directory: .../events.jsonl` — raw traceback for a durably-shipped job.
impact: the durable outcome (SHIP) is hidden behind a crash; an automation retry re-submits and duplicates the job.
Construction wraps are torture-14/15/16 F10/F12/F7 territory; this is the missing write-side sibling.
fix shape: LEARN/memory/catalog writes should be best-effort warn-and-continue AFTER the job verdict is durably committed
(never lose the shipped outcome), or the whole post-ship block wrapped so the CLI still prints the verdict line and the
server returns 200 with a warning field; either way a retry must not duplicate.

---

## FINDING 2 — high — `NINE_NODE_TIMEOUT_S=0` (or negative) raises ValueError AFTER the job is durably committed -> permanent zombies + raw tracebacks
area: env-var handling / CLI error paths
evidence:
- nine/runtime/workflows.py:66-85 `Node.__post_init__` — `int(_env_t)` accepted, then `< 1` -> `raise ValueError(
  "timeout_seconds must be >= 1 or None (got 0); 0 does NOT mean 'no timeout'")`. `None` = no timeout but the env
  cannot express it; a stale/typo'd 0 is an operator foot-gun, not a programming error.
- cli.py:271 `chain = CHAINS[job.workflow_id]()` and cli.py:293-294 `wf = WORKFLOWS[job.workflow_id]()` — construction
  is OUTSIDE the `except (WorkflowError, ValueError)` at cli.py:368; the ValueError IS caught there -> ONE clean
  [error] line, but cli.py:363-365 already durably submitted the job -> stuck in `submitted` forever
  (cmd_recover cli.py:555-559 accepts only blocked/failed; not 'running', so --force doesn't help either)
- cli.py:570-577 cmd_recover — `_execute_job` ValueError caught cleanly, but recover already transitioned the job to
  `recovered` AND wiped the job dir (cli.py:561-566) -> zombie at `recovered`
- cli.py:168 `chain = chains[args.chain_id]()` — OUTSIDE any try -> raw traceback (no zombie; nothing submitted yet)
- deploy/server.py:429 / 483 — chain/workflow construction outside try -> HTTP 500, job already submitted at 421-423 -> zombie
hermetic repros:
- Case C: NINE_NODE_TIMEOUT_S=0 -> `cmd_submit` rc=1 with clean `[error] job ... failed loud: node produce:
  timeout_seconds must be >= 1 or None (got 0)`; ledger lines=2, last status `submitted` (ZOMBIE confirmed)
- Case C2: =-5 -> identical, zombie at `submitted`
- chain repro: `nine chain flagship` with =0 -> raw `ValueError: node research: timeout_seconds must be >= 1...`
  propagated out of cmd_chain (would traceback), ledger lines=0 (no zombie)
fix shape: validate NINE_NODE_TIMEOUT_S at process entry BEFORE ledger.submit (fail fast, clean line, zero state
change), or treat <=0 as None ("no timeout" — the documented intent); wrap chain/wf construction in server + cmd_chain.

---

## FINDING 3 — medium — CLI lacks the server's OSError belt around executor.execute: an unreadable artifact file -> raw traceback + job stuck `running`
area: permission errors / CLI error paths (server/CLI asymmetry)
evidence:
- nine/runtime/workflows.py:622 `data = p.read_bytes()` in manifest registration — unwrapped; a chmod-000 (or
  EACCES/EIO/deleted-mid-run) artifact raises OSError out of execute()
- deploy/server.py:507-512 — server wraps `ex.execute` OSError -> `LedgerUnavailable` -> clean 502 (torture-18 F5)
- nine/cli.py:305 `result = executor.execute(wf, job, {"task": task})` — only `except WorkflowError` (306) -> OSError raw
- same on recover: cli.py:570-577 catches only (WorkflowError, ValueError) -> raw
- chains: cli.py:274/199 + server.py:446 catch only ChainError; ChainExecutor re-raises raw OSError (chain.py:139-147)
hermetic repro (Case B): bash node writes EVAL.json (passed:true) + RESULT.md then `chmod 000 RESULT.md`.
Result: `cmd_submit` RAISED `PermissionError: [Errno 13] Permission denied: .../RESULT.md`; ledger lines=4,
last status `running` (zombie; recover --force can salvage it — degraded, not unrecoverable).
fix shape: mirror the server's OSError->clean-line handling on the CLI _execute_job path (and mark the job
failed/blocked deterministically rather than leaving it 'running').

---

## Verified-holding surfaces (not re-filed)
- JSONLLedger `_append`/mkdir OSError->LedgerError wrap; corrupt-ledger-line tolerance (errors="replace",
  torture-5/6); registry dispatch gap (all 26 WORKFLOWS ids registered, submit/server refuse unregistered ids);
  EvidenceGate per-check try/except (file_nonempty_check stat TOCTOU safe); _check_command TOCTOU dismissed
  (too weak); Node construction OSError wraps (torture-14/15/16 F10/F12/F7) hold; cmd_memory/cmd_status/
  cmd_artifacts/cmd_cancel/cmd_recover all return clean error + exit 1; every CLI error path exits non-zero.
