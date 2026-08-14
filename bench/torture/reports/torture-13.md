# TORTURE-TESTER-13 Report — round 7: audit of slices 30–32 (recursive manifest, executor run-input snapshot, generalized stale guard + exemptions, chain fix_directive pop, ledger durable recover/cancel, cmd_recover validation order, bench_nine fresh-run/seed/killpg/exit-code)

Worker: TORTURE-TESTER-13 (round 7). Repo HEAD: 54a0c83 (slice 32, 2026-08-13).
All repros hermetic (no Gemini, no network, no quota): `.venv/bin/python` scripts under
/tmp/torture13/ (r1..r15) using the REAL modules + stub nodes / monkeypatches only; no
repo files touched (working-tree `bench/state.json` change is the loop's own spawn
bookkeeping, verified via git diff), no git operations.

Audited surfaces, all re-verified as HOLDING (each with a hermetic repro):
- t12-F1 executor-level run-input snapshot on the chain path (r1): a hop retry whose
  node produces NOTHING BLOCKs with the stale-artifact summary; a retry that rewrites
  the certified files SHIPs. The fix works.
- t12-F2 fix_directive pop on hop SHIP (r2): hop2's prompt AND its ledger input carry
  no fix_directive after hop1 FIXed then SHIPped.
- t12-F3 ledger durable recover/cancel (r3): a stale-cache holder cannot re-recover or
  cancel a job another process durably SHIPped; shipped stays the terminal line; the
  cache is synced on successful recover.
- t12-F4 review hop writes review-eval.json (r10): build EVAL.json survives on disk
  byte-identical; review manifest has exactly one EVAL.json entry (its own
  review-eval.json) — no conflicting duplicate.
- t12-F5 cmd_recover validation order (r4): chain-hop ids and unregistered ids are
  refused BEFORE the wipe/transition (artifacts + status intact, rc 1, one clean line);
  missing task.txt still refuses cleanly; `recover --force` on a stale running job is a
  one-call degrade (r14).
- t12-F6/F7 registry (r12/r13): dead catalog keyword ids dropped with a loud warning;
  `nine submit` refuses a routed-but-unregistered id before submitting; broken plugin
  registry warns loudly.
- t12-F8 ledger-construction OSError (r4b): discover/stats/status/cancel/recover/
  artifacts all print one clean `error:` line on an unusable --ledger path.
- t11-F1 fresh-run wipe + seed_worker new-dir (r6): main() wipes the fixture run root;
  the seeder seeds the NEW job dir and returns only for it (stale dirs cannot satisfy).
- t11-F2/F3 verify collection detection + `-q` summary parse (r9): a failing run whose
  test names contain `valueerror` reports `N test(s) failed, M passed`, never a false
  "pytest collection error"; collection errors (rc 5 / `ERROR collecting`) still detected.
- t11-F4 check.sh timeout (r15): TimeoutExpired is caught into a timed_out row — one
  hanging candidate cannot abort the bench.
- t11-F5 recursive manifest (r5 cases 1–3): subdir files (reviews/security.md) and
  directory artifacts (solution/) get per-attempt provenance with relative names; stale
  attempt-1 subdir/dir content BLOCKs; rewrites SHIP.
- t11-F6 exit code (r6): 0/2 SHIP → rc 1 with SCORE banner; 2/2 SHIP → rc 0.
- t11-F7 convert_to_pytest warning (r8/r8b): nested test() calls warn loudly (and
  all-nested runners raise), top-level calls still convert 1:1.
- t11-F8 starter scoring (r6b): a BLOCKed fixture whose candidate is byte-identical to
  the broken starter is NOT scored (0/0, flagged), verify not even called.

Findings below are NEW: the `nine recover <unknown-id>` raw traceback survived the
slice-32 clean-error hardening (cli.py:399 is outside every try), the bench killpg
timeout fix cannot reach the runtime's DETACHED bash-node process groups (the ghost
writer the BONUS fix claims to kill still survives), the run-input exemption covers
files the RUN MODIFIED (a gate-certified input rewritten in attempt 1 can SHIP on a
later attempt while absent from the shipped manifest — the t12-F1 family, still open
through the exemption boundary), the explicit artifact_path dedup key is a basename
while the manifest is relative (subdir artifact_path → duplicate entries), and the
per-run results archive skips the documented default RUNID r0.

---

## FINDING 1
- area: CLI clean-error contract (ledger/recover)
- severity: low
- title: `nine recover <unknown-job-id>` still raw-tracebacks — cli.py:399
  `job = ledger.get(args.job_id)` sits OUTSIDE every try/except, so the slice-32 F8
  claim ("discover/stats/submit/chain/recover catch it with one clean line") is false
  for recover's job-get path (and T2-F7's "recover unknown id -> clean one-line error"
  is regressed/never-fully-fixed)
- evidence: `/tmp/torture13/r4_recover_order.py` + `r4b_cli_matrix.py` (real CLI):
  ```
  status/cancel/artifacts <unknown-id> -> rc 1, "error: job not found: deadbeef" (clean)
  recover <unknown-id>                 -> rc 1, raw traceback:
      nine.ledger.ledger.LedgerError: job not found: deadbeef-dead-beef-dead-beefdeadbeef
  ```
  `cmd_recover` (cli.py:379) wraps `_ledger(args)` (torture-12 F8) but not the
  `ledger.get` at line 399 that runs before the new workflow-id validation; `main()`
  has no global handler. The unusable-ledger matrix (all 6 commands) is clean.
- impact: operators who typo a job id on recover get a full Python traceback instead
  of the project's one-line `error:` contract; automation keying on stderr shape
  breaks. Functionally still rc 1, so this is a contract/noise violation, not a state
  bug — but it is exactly the class the slice-32 commit message claims to have closed.
- suggested_fix: wrap `job = ledger.get(args.job_id)` in the same
  `except LedgerError -> print("error: ...") -> return 1` pattern used by
  cmd_status/artifacts/cancel. Regression test: `nine recover <unknown-id>` asserts
  rc 1, one clean error line, no "Traceback".
- effort: S

## FINDING 2
- area: bench (bench_nine.py) / runtime process groups
- severity: medium
- title: The BONUS (torture-11) killpg timeout fix cannot kill the ghost writer it
  was built for — the runtime's bash nodes run `start_new_session=True` (their OWN
  process group), so `os.killpg(proc.pid, ...)` at bench_nine.py:242-250 reaches only
  the nine CLI itself; the detached verify/pytest tree survives the timeout and keeps
  writing into the abandoned job dir
- evidence: `/tmp/torture13/r7_ghost_killpg.py` reproduces the exact three-level
  topology with the EXACT slice-32 timeout code (bench Popen start_new_session →
  CLI → bash node with the runtime's verbatim `start_new_session=True` spawn
  (workflows.py:224) → pytest grandchild):
  ```
  CLI (session leader) rc after bench killpg path: -15     <- CLI dies promptly
  GHOST_MARKER written by the pytest grandchild: True      <- ghost SURVIVES
  surviving ghost pids: [10472, 10473]
  ```
  `proc.kill()` was replaced by `killpg(proc.pid)` (bench_nine.py:244/248), but
  `proc.pid` is the CLI's group; the runtime deliberately detaches every bash node
  into its own session/group (workflows.py `_run_node_once`,
  `sp.Popen(..., start_new_session=True)`) so the node + its pytest child are NOT in
  the CLI's group. After the CLI dies, the runtime's own timeout logic (which would
  killpg the node group) dies with it, so a hanging pytest (e.g. a patch that loops
  forever on a test input — the exact pathological case) becomes a permanent ghost
  writer. The change is equivalent to `proc.terminate()` for the ghost problem.
- impact: the BONUS bug it claims to fix (ghost writers landing files in abandoned
  bench job dirs after a per-fixture timeout) is still live; a pathological patch can
  leave a permanently-running pytest on the bench host per timed-out fixture, and the
  fix's regression coverage does not exist (no test exercises the timeout branch at
  all — grep test_torture_harvest_7.py: no killpg/timeout test).
- suggested_fix: track the bash-node process groups (the runtime knows each node's
  pid/group — expose them, e.g. job metadata) and kill every descendant group on
  timeout, or run the CLI in a cgroup/subreaper (prctl PR_SET_CHILD_SUBREAPER +
  recursive descendant kill), or have the runtime record node pids in a file the bench
  can read after the timeout. Regression test: a workflow whose bash node spawns a
  detached sleeper writing a marker; bench timeout path asserts the marker is never
  written and no descendant pid survives.
- effort: M

## FINDING 3
- area: stale-artifact guard / run-input exemption boundary (nine/runtime/workflows.py)
- severity: medium
- title: The run-input exemption covers files the RUN MODIFIED — a gate-certified file
  present in the attempt-1 snapshot stays exempt FOREVER, so a later attempt that
  produces nothing SHIPs on attempt-1-MODIFIED content that is ABSENT from the shipped
  manifest (the exact t12-F1 integrity class, still open through the exemption)
- evidence: `/tmp/torture13/r5_recursive_manifest.py` case 4 (real executor, real
  gate; `seeded.md`+`required.txt` pre-seeded as run inputs, both gate-certified):
  ```
  attempt 1: node REWRITES seeded.md + required.txt (a real fix)
  attempt 2: node produces only ok.flag
  verdict: SHIP
  shipped manifest names: ['ok.flag']                      <- certified files MISSING
  disk seeded.md content: 'FIXED content ...'              <- attempt-1-modified content certified
  ```
  `inputs_ok = self._first_attempt_before.get(str(job_dir))` (workflows.py:542) is the
  attempt-1 file-name set; the guard at workflows.py:551-553 exempts `expected_name in
  inputs_ok` regardless of whether the run rewrote the file in an earlier attempt (the
  same scenario with NO seed BLOCKs — r5 case 1 — proving the exemption is the only
  difference). Chain variant: hop N+1's attempt-1 snapshot contains hop N's artifacts;
  if hop N+1's gate certifies one of them (plan_hop's `handoff-md` check certifies
  HANDOFF.md, flagship.py) and the hop model rewrites it, the chain rollup carries hop
  N's ORIGINAL sha while the disk file holds the modified bytes — manifest-vs-disk
  divergence on the shipped chain job.
- impact: "SHIP must have produced its evidence THIS attempt" is still violable for the
  seeded-run-input shape the bench and chains actually use; the shipped manifest can
  omit a file the gate certified, so evidence replay (`nine artifacts`, re-verification,
  memory lineage) cannot prove the certified content is the shipped content. The fix
  needs a per-file rule, not a per-name exemption.
- suggested_fix: keep the exemption only while the file is UNCHANGED since the
  attempt-1 snapshot — snapshot each input's (size, mtime, sha) at attempt 1, and once
  an attempt modifies a run input, treat it as run-produced for all later attempts (it
  must then be re-registered in the shipping attempt's manifest). Regression test:
  pre-seed a gate-certified file, rewrite it in attempt 1, empty attempt 2 → BLOCK with
  the stale summary naming the file; unchanged-seeded-input case still SHIPs.
- effort: S

## FINDING 4
- area: recursive manifest / tool-node artifact registration (nine/runtime/workflows.py)
- severity: low
- title: Explicit `artifact_path`/`artifact` dedup still keys on the BASENAME
  (`seen.get(p.name)`, workflows.py:493) while the recursive manifest now keys on
  RELATIVE names — a subdir artifact returned via artifact_path is registered TWICE
  (same name/path/sha), the t12-F4 "two conflicting manifest entries" class for
  same-hash duplicates
- evidence: `/tmp/torture13/r11_tool_dup.py` — node writes docs/README.md and returns
  `{"artifact_path": ".../docs/README.md"}`:
  ```
  manifest entries: ['docs/README.md', 'docs/README.md']
  ```
  The rglob loop registers `docs/README.md` (rel key); the explicit loop then checks
  `seen.get(p.name)` == `seen.get("README.md")` → miss → registers the identical
  entry again. Built-in nodes only return top-level paths (ADK agent_output.md,
  responder/draft_email/ideate targets), so today this is reachable only via plugin /
  compose lanes that certify a subdir file — but those are exactly the lanes the
  recursive manifest (t11-F5) was built to support, and the duplicates flow into chain
  hop rollups and `nine artifacts`.
- impact: duplicate manifest entries for the same artifact (name+hash identical) —
  noisy `nine artifacts`, inflated rollups, and any consumer that counts entries (the
  t12-F4 fix made the flagship chain assert single-EVAL semantics) can misbehave on
  plugin lanes.
- suggested_fix: compute `rel` before the dedup check and use `seen.get(rel)` (the
  relative key is already computed two lines later); the basename fallback only for
  paths outside the job dir. Regression test: node returns artifact_path to a subdir
  file → assert exactly one manifest entry for it.
- effort: S

## FINDING 5
- area: bench scoreboard / regression archive (bench_nine.py)
- severity: low
- title: The per-run results archive skips the DOCUMENTED DEFAULT RUNID — main() only
  copies `results-<RUNID>.json` when `RUNID != "r0"` (bench_nine.py:435), so the
  t11-F6 gap (results.json silently overwritten with no comparison source under the
  documented default invocation) remains for every default run
- evidence: `/tmp/torture13/r6_bench.py`:
  ```
  NINE_BENCH_RUNID=r0 (default): results-r0.json archived: False
  NINE_BENCH_RUNID=r1          : results-r1.json archived: True
  ```
  The F6 fix archives only explicitly-named runs; `python bench/bench_nine.py` (the
  documented invocation, RUNID default "r0") still destroys the previous run's
  scoreboard with no archive — the exact "regression is invisible" scenario F6 was
  filed for. The existing harvest-7 regression test
  (`test_t11_f6_bench_exit_code_and_archive`) also uses a non-default RUNID, so the
  gap is untested.
- impact: the loop's "pass rate must never go down" rule still has no automated
  comparison source for default runs; a 9/9→0/9 collapse under the default invocation
  is recorded and overwritten without an archive. Minor (exit code now fails on
  <100% SHIP, which catches the collapse at least), but the archive promise in the
  commit message ("per-run results archived") is not met for r0.
- suggested_fix: archive unconditionally (e.g. `results-<RUNID>-<ts>.json`), or treat
  r0 like any other id. Regression test: run main() with RUNID="r0" and assert a
  results-r0 archive exists.
- effort: S

---

## Round summary
5 new findings (1 medium on the killpg ghost, 1 medium on the run-input exemption
boundary, 3 low: recover unknown-id traceback, artifact_path duplicate registration,
r0 archive skip). 14 re-verified fix surfaces from t11/t12 all hold (list above). All
repros hermetic; no repo files modified. Repro scripts kept in /tmp/torture13/
(r1..r15) for triage.
