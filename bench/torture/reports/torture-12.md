# TORTURE-TESTER-12 Report — round 6: flagship hop DAG + ledger JSONL/recover + plugin merge guard + chain gates/stale guard + CLI exit codes/self-test

Worker: TORTURE-TESTER-12 (round 6). Repo HEAD: 494090d (slice 31).
All repros hermetic (no Gemini, no network, no quota): `.venv/bin/python` scripts under /tmp,
stub/monkeypatch only, no repo files touched, no git operations. Full repro scripts kept in
/tmp/torture12/ (g1..g8 files) for triage.

Re-attacked surfaces that HOLD (not re-filed): hop FIX-loop fresh-job-per-attempt (chain.py),
cross-process cancel polling (T8-F3), per-attempt manifest snapshot + T10-F2 stale guard on the
single-workflow path, write containment, symlink non-evidence in gate/manifest, plugin-vs-core
id collision skip (T10-F4), recover task.txt/symlink guards. Findings below are NEW: the stale
guard is reset per chain hop attempt (chain SHIPs stale evidence the single-workflow path
blocks), the chain FIX directive bleeds into later hops, the flagship review hop clobbers the
build EVAL.json on disk + ships two conflicting manifest entries, recover() validates the
stale in-memory cache instead of the durable file, plugin-registry load failures are silent,
router keywords can route to un-executable ids (CLI then raw-tracebacks), recovering a chain
HOP job raw-tracebacks and tombstones it at `recovered`, and ledger-construction OSErrors
raw-traceback every CLI command.

---

## FINDING 1
- area: chain gates + stale guard (flagship hop DAG)
- severity: high
- title: The T10-F2 stale guard is reset on every chain hop FIX attempt — a hop retry whose model produces NOTHING SHIPs on the previous attempt's artifacts (the exact bug T7-F1/T10-F2 fixed for single workflows, still open on the chain path)
- evidence: `nine/chains/chain.py:203` creates a FRESH `WorkflowExecutor` inside the hop loop (`ex = WorkflowExecutor(...)`), so every hop attempt is a new executor whose `first_attempt_before` snapshot (`nine/runtime/workflows.py:362`) is taken AFTER the previous attempt's files were written. The stale guard's run-input exemption `inputs_ok = first_attempt_before or set()` (`workflows.py:508`) then treats the PREVIOUS attempt's produced files as legitimate run inputs and skips them. Repro (`/tmp/torture12/g8c_chain_stale.py`, flagship research-hop gate shape: research-md + handoff-md + research-nonempty, NO eval-json check): attempt 1 writes research.md+HANDOFF.md but fails a flag check; attempt 2 (model produced nothing, only writes an unrelated FLAG2 file):
  ```
  CHAIN path final: SHIPPED          <- research.md/HANDOFF.md are attempt-1 files
  SINGLE-workflow final: BLOCK
  summary: stale artifact(s): ['HANDOFF.md', 'research.md'] - the gate passed on
           file(s) not produced this attempt ...
  ```
  Same node + same gate, only the executor differs. The flagship research hop is exactly this gate shape (`flagship.py:123-129`), so a research retry whose ADK agent emits no file SHIPs attempt-1 findings as fresh; `tests/test_chains.py` never exercises a hop retry where the model writes nothing.
- impact: certified artifacts in a shipped chain can be stale evidence from a failed attempt — the precise integrity failure the stale guard was built for, still reachable on the flagship path. A model hiccup (empty response, tool-call misfire) that previously BLOCKed now SHIPs with replayed findings/plan.
- suggested_fix: in `ChainExecutor`, carry the FIRST-attempt snapshot across hop retries (compute `first_attempt_before` once per hop and pass it into the executor, or seed the new executor with the hop's original job-dir snapshot). Regression test: chain hop whose attempt-2 node writes nothing must BLOCK with the stale-artifact summary, mirroring the single-workflow test.
- effort: S

## FINDING 2
- area: chains (flagship hop DAG)
- severity: high
- title: Chain FIX-directive bleeds into EVERY later hop — after hop N FIXes and then SHIPs, hops N+1..end receive hop N's stale "Previous attempt failed the gate" directive in their model prompts
- evidence: `nine/chains/chain.py:267` sets `chain_inputs["fix_directive"] = ...` inside the hop FIX loop and never clears it; the next hop's `self.ledger.submit(wf_id, input=dict(chain_inputs), ...)` (`chain.py:212`) carries it, and every flagship ADK node reads `inputs.get("fix_directive")` and appends "Previous attempt failed the gate: ..." to the instruction (`flagship.py` `_fix_directive_suffix`). Repro (`/tmp/torture12/g1_fixdir_bleed.py`): hop1 fails its gate once then SHIPs; hop2 records the fix_directive it received:
  ```
  final: SHIPPED
  hop1 attempts: 2
  hop2 received fix_directive: 'hop hop1 failed gate (attempt 1): gate checks failed; rework and re-run.'
  ```
  Hop2's directive should be "" — hop1 SHIPPED. Zero tests assert hop inputs on later hops.
- impact: on any flagship run where an early hop needed a FIX, the plan/build/review/teach prompts all falsely claim "Previous attempt failed the gate: hop research/plan ..." — misleading instructions that can make later models rework unrelated artifacts (wasted Gemini budget) or contaminate the review/teach output. The chain prompt contract lies.
- suggested_fix: reset `chain_inputs["fix_directive"] = ""` (or pop it) when a hop SHIPs, before the next hop submits. Regression test: 2-hop chain, hop1 FIXes then SHIPs, assert hop2's submitted input has no fix_directive.
- effort: S

## FINDING 3
- area: ledger JSONL semantics + recover (cross-process)
- severity: high
- title: `JSONLLedger.recover()` validates the in-memory cache, not the durable file — a stale cache can re-recover a job another process already SHIPPED (recovered/running lines stamped over the shipped terminal line)
- evidence: `nine/ledger/ledger.py:310-323` — `recover()` checks `job.status not in ("blocked","failed")` against `self.get(job_id)` = the construction-time cache (`_load` last-line-wins), NOT the durable file; only the `--force` branch of `cmd_recover` (`cli.py`) refreshes. The T8-F3/T10-F1 fixes made executors poll the durable state, but the recover legality check itself never does. Repro (`/tmp/torture12/g3_recover_stale.py`) — long-lived process B constructed its ledger while the job was blocked; another process then durably SHIPs it; B calls recover():
  ```
  A: job status now: blocked
  durable status (fresh read): shipped
  B's cache still says: blocked
  B.recover() SUCCEEDED on a durably-SHIPPED job (GAP)
  durable status after B.recover: recovered      <- shipped line buried (last-line-wins)
  ```
  In the CLI this hits when two recover processes race or any long-lived holder (the deploy server's global `_ledger`, daemon agents) calls recover: a verified job's artifacts get wiped and re-executed, double model spend, and the durable replay history shows shipped->recovered->running (a lie). `cmd_cancel` shares the same stale-cache legality check.
- impact: cross-process data integrity — the exact race family T8-F3/T10-F1 addressed, still open on the recover/cancel control plane. Re-execution of shipped/cancelled work and corrupted replay history.
- suggested_fix: `recover()` (and `cmd_recover` non-force) should `refresh()` the durable record first and re-check blocked/failed against it (mirror the --force branch); add a regression test with two JSONLLedger instances where the durable status is shipped and assert recover refuses.
- effort: S

## FINDING 4
- area: chains (flagship hop DAG) / ledger manifest semantics
- severity: medium
- title: The flagship review hop CLOBBERS the build hop's EVAL.json on disk and the shipped chain manifest ends up with two conflicting EVAL.json entries (same name, different sha256, different producers)
- evidence: build hop's self-test writes `EVAL.json` (`flagship.py` `_build_self_test_command`); the review hop's `_review_eval_command` (`flagship.py:404-427`) writes the SAME filename with the review's own EVAL.json (`printf ... > EVAL.json`). ChainExecutor rolls each hop's manifest into the container job (`chain.py:288`). Repro (full flagship chain, fake models, `/tmp/torture12/g2_eval_clobber.py`):
  ```
  chain manifest EVAL.json entries: 2
     sha256: 9f9ff232131d6011 produced_by: self-test  size: 91   <- build hop's self-test evidence
     sha256: 6abc601ea39a0425 produced_by: review-eval size: 95  <- review hop's verdict
  on-disk EVAL.json now: {"checks":[{"name":"review-pass",...}],...}   <- build's EVAL.json is GONE from disk
  ```
  `tests/test_chains.py::test_flagship_chain_ships_all_hops` asserts a NAME SET (`<= names`), so the duplicate passes silently. Any consumer re-reading `work/<job>/EVAL.json` after the chain (re-verification, operator QA, memory replay) sees the review's EVAL.json, not the build evidence the review hop certified.
- impact: the shipped job dir's certifying evidence for the build is destroyed on disk; the manifest is self-contradictory (one name, two hashes); `nine artifacts` shows both with no provenance note. Evidence replay/verification tools cannot trust either the disk file or the manifest entry list.
- suggested_fix: give the review hop a distinct EVAL filename (e.g. `REVIEW_EVAL.json`) for its own gate, or keep the build EVAL.json read-only and have the review write a separate evidence file; add a regression assert that the chain manifest has exactly one EVAL.json and it matches the build self-test sha.
- effort: S

## FINDING 5
- area: ledger JSONL semantics + CLI (recover of chain hop jobs)
- severity: medium
- title: `nine recover <chain-hop-job-id>` raw-tracebacks with "unregistered workflow id" AFTER wiping the job dir and tombstoning the job at `recovered` (recover of a hop job is a dead-end)
- evidence: chain hop jobs get workflow_id `"<chain-id>::<hop-id>"` (`chain.py:209`); `_execute_job` (`cli.py:234-236`) raises `WorkflowError` for any id not in WORKFLOWS/CHAINS, and `cmd_recover` (`cli.py:425`) calls `_execute_job` with NO catch (T7-F5 only wrapped the ChainError path). Repro (`/tmp/torture12/g6_hop_recover.py`) — a blocked chain hop job recovered via cmd_recover:
  ```
  recovering a5d1f361... (research-plan-build-review-teach::build) — re-executing
  Traceback ... nine.runtime.workflows.WorkflowError:
  unregistered workflow id 'research-plan-build-review-teach::build' — no collect fallback ...
  ```
  The wipe (`cmd_recover` deletes the job dir contents) already ran, and `ledger.recover()` already transitioned the hop job to `recovered` — which is NOT in the recover-able set (`ledger.py:319` blocked/failed only), so the job is stuck at `recovered` forever with its artifacts deleted. Very reachable: after a chain BLOCK, `nine discover --status blocked` lists the hop jobs and operators will naturally recover the visible hop job.
- impact: raw traceback (violates the clean-error pattern T2-F7/T7-F5), plus destroyed artifacts and an unrecoverable `recovered` tombstone in the ledger for the hop job.
- suggested_fix: catch WorkflowError in `cmd_recover` (and `cmd_submit`) with a clean one-line error; for `::`-suffixed hop ids, print "recover the parent chain job <chain-id> instead". Regression test: recover a `chain::hop` job → rc 1, one clean error line, no traceback, artifacts intact, job still blocked/failed.
- effort: S

## FINDING 6
- area: plugin merge collision guard / registry consistency
- severity: medium
- title: Router keywords can point at workflow ids that are NOT executable — a learned/catalog keyword for a removed plugin id routes `nine submit` into a raw `WorkflowError` traceback, and nothing validates the routing registry against the execution registry
- evidence: `_merged_keywords()` (`registry.py:294`) merges catalog overrides into KEYWORDS with no check that the id exists in WORKFLOWS; `build_default_router()` (cli.py) registers every KEYWORDS id verbatim, and `Router.classify` only validates against that keyword catalog — never against WORKFLOWS. A plugin id that got a keyword via `nine learn apply` becomes a dead route the moment the plugin registry disappears (which itself happens SILENTLY — see Finding 7). Repro (`/tmp/torture12/g5b_dead_id.py` + `g5c_cli_dead.py`): catalog override `ghost_plugin -> ["triage issues"]`, plugin gone:
  ```
  ghost_plugin in merged KEYWORDS: True
  ghost_plugin in executable WORKFLOWS: False
  router decision workflow_id: ghost_plugin
  cmd_submit UNCAUGHT: WorkflowError: unregistered workflow id 'ghost_plugin' ...
  ```
  The server returns a clean 502 (exception handler); the CLI raw-tracebacks (no WorkflowError catch in cmd_submit — the same hole as Finding 5).
- impact: every submit matching a learned keyword for a removed/renamed lane is a guaranteed-failing job with a confusing error; routing metadata (decision.workflow_id) lies about what will run. The collision guard (T10-F4) protects core ids, but nothing protects against STALE plugin ids.
- suggested_fix: validate KEYWORDS against WORKFLOWS∪CHAINS at merge time (drop/warn on unknown ids, like load_catalog does), and make `_apply_candidate` refuse workflow ids not in WORKFLOWS. Regression test: catalog keyword for a dead id is dropped with a warning and the router cannot emit it.
- effort: S

## FINDING 7
- area: plugin merge collision guard (registry robustness)
- severity: low
- title: A broken plugin registry file (syntax error / import failure) silently disables EVERY plugin lane with zero warning — operators believe composed lanes are live
- evidence: `_load_plugin_workflows` (`registry.py:165-190`) wraps `spec.loader.exec_module(mod)` in `except Exception: return {}` with no stderr message (contrast: `load_catalog` prints loud warnings for corrupt catalogs, `registry.py` T4-F3/T6-F6). Repro (`/tmp/torture12/g4_plugin_silent.py`): NINE_PLUGIN_REGISTRY pointing at a file containing a syntax error:
  ```
  plugin workflows loaded: {}
  (stderr empty — no warning at all)
  ```
- impact: after a compose run writes a malformed registry (crash mid-append, hand-edit, version skew), every plugin lane silently vanishes at import; combined with Finding 6, learned keywords then route into guaranteed-failing jobs. The T10-F4 collision guard is moot because the failure is invisible.
- suggested_fix: emit a loud `warning: plugin registry <path> failed to load (<exc>); plugin lanes disabled` to stderr (mirror load_catalog), and keep the idempotent {} return. Regression test: assert the warning string on a broken registry.
- effort: S

## FINDING 8
- area: CLI exit codes / self-test robustness
- severity: low
- title: Ledger-construction OSError escapes every CLI command — `nine discover|stats|cancel|status|artifacts|submit|chain|recover` with an unusable `--ledger` path raw-tracebacks instead of the project's clean one-line error
- evidence: `JSONLLedger.__init__` (`ledger.py:162`) runs `self.path.parent.mkdir(parents=True, exist_ok=True)` OUTSIDE the `try/except OSError -> LedgerError` that wraps `_load`; the CLI helpers only catch `LedgerError` (`cmd_status/artifacts/cancel`), and `cmd_discover/stats/submit/chain/recover` catch nothing around `_ledger(args)`. Repro (`/tmp/torture12/g7_ledger_oserror.py`) — `--ledger` parent is a regular file:
  ```
  discover -> UNCAUGHT FileExistsError : [Errno 17] File exists: '.../not-a-dir'
  stats    -> UNCAUGHT FileExistsError : [Errno 17] File exists: '.../not-a-dir'
  cancel   -> UNCAUGHT FileExistsError : [Errno 17] File exists: '.../not-a-dir'
  ```
  Same for read-only parents (PermissionError). The T2-F7/T4-F2/T6-F8 clean-error pattern is not applied to construction.
- impact: any operator typo or permissions issue on `--ledger`/default `jobs/` produces a raw Python traceback for every subcommand; automation keying on exit codes still gets exit 1 but with noise, and the "error:" prefix contract is broken.
- suggested_fix: move the mkdir inside the try (or wrap it separately as LedgerError), and give `main()` a single `except (LedgerError, OSError)` -> clean one-line error + rc 1. Regression test: `nine --ledger <file>/ledger.jsonl discover` prints one clean line, no "Traceback".
- effort: S

---

## Round summary
8 new findings (2 high on the flagship/chain stale-guard family, 1 high on recover durability, 1 medium on the review-EVAL clobber, 4 medium/low on registry/CLI robustness). All repros hermetic; no repo files modified. Surfaces re-checked and holding: hop FIX fresh-job semantics, T8-F3 cancel polling, single-workflow stale guard, write containment, symlink non-evidence, plugin-vs-core collision skip, recover task.txt/symlink guards.
