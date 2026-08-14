# TORTURE-TESTER-11 Report — bench + gate machinery (round 6)

Worker: TORTURE-TESTER-11 (round 6, attack surface: bench harness + gate machinery —
`bench/bench_nine.py`, `nine/gates/evidence.py`, `convert_to_pytest`, stale-artifact
guard, debug-lane fix loop, bench scoreboard).
Repo HEAD: 494090d (slice 31, first full 9/9 SHIP sweep).
All repros hermetic (no Gemini, no network, no quota): standalone scripts in /tmp run
with the repo's `.venv/bin/python`; stubs/monkeypatches only; no repo files touched,
no git operations (working-tree `bench/state.json` change belongs to the loop, not
this worker).

Re-attacked surfaces that HOLD (not re-filed): strict-boolean EVAL gate (a72d43f),
symlink-never-evidence in gate + manifest (T6-F1/T8-F1), gate `.expected` provenance
tags on the three built-in check factories (T10-F2), stale-guard top-level-file logic
for EVAL.json/required artifacts, convert_to_pytest round-trip for all 9 current
fixtures (verified via `pytest --collect-only` against stubbed solutions: collected
count == def count for every fixture 001-009).

Findings below are NEW. The bench+gate cluster has real structural gaps: the seeder
breaks repeat runs, the verify node fabricates EVAL evidence on failing runs, the
independent check can abort the whole bench, the stale guard exempts subdir/directory
artifacts, the scoreboard cannot fail, convert_to_pytest silently drops non-top-level
tests, BLOCKed fixtures get scored on the broken starter, and the per-fixture timeout
orphans nine's detached bash-node children.

---

## FINDING 1
- area: bench (bench_nine.py)
- severity: high
- title: `seed_worker` returns on a STALE job dir from a previous run — every repeat
  bench run with the same RUNID runs every fixture UNSEEDED and BLOCKs 9/9
- evidence: `bench/bench_nine.py:152-171` — `seed_worker` iterates `workdir.iterdir()`
  and returns the moment ANY job dir has both `solution.py` and `test_solution.py`
  (`bench_nine.py:169`). `main()` never cleans the workdir: `workdir.mkdir(parents=True,
  exist_ok=True)` (`bench_nine.py:315`) under `bench/runs/<fx>/run-{RUNID}/work`, and
  RUNID defaults to `"r0"` — the documented invocation (`python bench/bench_nine.py`,
  docs/IMPROVEMENT_LOOP.md) is exactly the repeat case. On the SECOND run the workdir
  already contains run-1's job dirs (with both seeded files), so the seeder's first
  poll sees the stale dir and returns BEFORE the new job dir even exists.
  Repro (hermetic, `/tmp/reproB.py`, real `seed_worker`): stale dir `stale-job-1111`
  with both files pre-created; fresh dir `fresh-job-2222` created 0.3 s later:
  ```
  fresh solution.py exists: False      <- new job NEVER seeded
  fresh test_solution.py exists: False
  ```
  The debug lane's verify node then finds no `test_solution.py` → `patch-runs` branch →
  `passed:false` → FIX×2 → BLOCK for every fixture, while the `[warn]` path never even
  fires (the seeder did not hit its 60 s deadline; it returned early).
- impact: the bench harness self-destructs on its SECOND documented run: 9/9 SHIP
  becomes 9/9 BLOCK with zero diagnostic (results rows just say BLOCK; the only clue is
  `tests=0/0` + `expected_tests` still populated). Any slice that re-runs the bench
  (the loop's core cycle) with the default RUNID gets a fake regression.
- suggested_fix: wipe/recreate the per-fixture `work/` dir at the start of `main()`
  (it is transient by design — `bench/runs/.gitignore` says so), and make `seed_worker`
  seed only job dirs created AFTER the seeder started (record a workdir snapshot at
  seeder start, or key off the newest dir). Regression test: two sequential
  `seed_worker` invocations over one workdir — assert the second run's fresh dir
  receives the files.
- effort: S

## FINDING 2
- area: debug lane fix loop (nine/workflows/debug_wf.py `_build_verify_command`)
- severity: high
- title: Every failing pytest run whose output contains `error` (fixture 002/003 test
  names include `raises_value_error`; any traceback text) is misclassified as
  "pytest collection error" — false EVAL evidence + a fix_directive that steers the
  patch agent at a non-existent collection bug
- evidence: `debug_wf.py:201` — `if grep -qE 'error|no tests ran|collection'
  test_output.log` runs BEFORE the rc checks. pytest `-q` failure output always ends
  with `FAILED test_patch.py::test_NN_<slug> - <reason>` lines; slugged test names from
  fixture 002 (`missing_raises_valueerror`, `multiple_raises_valueerror`,
  `empty_string_raises_valueerror`, `whitespace_only_string_raises_valueerror`) and
  003 (`invalid_token_raises_valueerror`, `mixed_valid_invalid_raises_valueerror`)
  contain the substring `error` (via `valueerror`), so ANY failing run of those
  fixtures matches. Repro (hermetic, `/tmp/reproA.py` — the verbatim verify command
  against the converted test_solution.py + broken starter for fixtures 001/002/006):
  ```
  == bugfix-small-002: verify_rc=0 log_FAILED_lines=7 log_AssertionError=4
     EVAL message: pytest collection error      <- 7 tests merely failed assertions
  == bugfix-small-001: verify_rc=0 ... EVAL message: 6 test(s) failed, 0 passed
  ```
  The gate then builds `fix_directive` verbatim from this message
  (`workflows.py:558-567`), so the patch agent is told "pytest collection error" and
  hunts a collection/import bug that does not exist. Fixture 002 was the fixture that
  took 2 attempts in the slice-31 sweep — this misdirection is live in real runs.
- impact: the debug lane's fix loop — the exact mechanism the bench drives — feeds the
  patcher a false root cause on every failing attempt of fixtures 002/003, burning
  fix-loop budget and model calls; the EVAL.json evidence record lies ("collection
  error") and is stored as the job's certified evidence.
- suggested_fix: only treat rc==5 / `no tests ran` / an explicit collection marker as a
  collection error; match pytest's structured signals (e.g.
  `grep -qE '^ERROR|no tests ran|error in collecting'`), never the bare word `error`
  in the whole log. Regression test: run the verify command on a failing assertion run
  whose test names contain "error" and assert the EVAL message says "test(s) failed",
  not "collection error".
- effort: S

## FINDING 3
- area: debug lane fix loop / EVAL evidence truth
- severity: medium
- title: verify node's failed-count message always reports `0 passed` — `grep -c '
  PASSED'` is case-sensitive and pytest `-q` output never contains ` PASSED`
- evidence: `debug_wf.py:210-211` — `failed=$(grep -c 'FAILED' ...)` and `passed=$(grep
  -c ' PASSED' ...)`. pytest `-q` prints passing tests as dots; ` PASSED` tokens only
  appear in `-v` mode. Repro (same `/tmp/reproA.py`): fixture 001's broken starter,
  pytest reports `6 failed, 3 passed in 0.10s`:
  ```
  EVAL message: 6 test(s) failed, 0 passed     <- real summary says 3 passed
  ```
  `FAILED` counts DO work (the summary lines match), so the failure count is right but
  the pass count is always 0 — every failed-attempt EVAL.json certified by the gate
  contains a false statement about how many tests passed.
- impact: EVAL.json is the job's certified evidence record; a persistent false
  `0 passed` undercuts the "evidence before SHIP" doctrine and misleads operators/loop
  readers of verdict summaries. The gate verdict itself is unaffected (still FIX), so
  this is an evidence-truth bug, not a SHIP lie.
- suggested_fix: parse the pytest summary line instead (`grep -oE '[0-9]+ passed'` and
  `[0-9]+ failed`) or run pytest with `-q --tb=line` and count `FAILED` lines while
  deriving passed from the summary; add a regression test asserting a 6-fail/3-pass run
  produces `"6 test(s) failed, 3 passed"`.
- effort: S

## FINDING 4
- area: bench (bench_nine.py) resilience
- severity: medium
- title: `verify_with_check_sh` lets `subprocess.TimeoutExpired` escape — one hanging
  candidate patch.py (infinite loop) aborts the ENTIRE bench run and no results.json is
  ever written
- evidence: `bench_nine.py:246-254` — `subprocess.run([...], timeout=120)` inside
  `verify_with_check_sh` has no try/except; `main()` calls it at `bench_nine.py:327`
  with no guard, and results are persisted only at the very end (`bench_nine.py:363`).
  Repro (hermetic, `/tmp/reproC.py` — monkeypatched `subprocess.run` raising
  `TimeoutExpired`, plus the real call site):
  ```
  UNCAUGHT TimeoutExpired ESCAPED verify_with_check_sh: ... timed out after 120 seconds
  ```
  Real trigger: the debug lane's patch agent writes `patch.py`; `verify_with_check_sh`
  runs the fixture's check.sh against it — a patch that loops forever on a test input
  hangs the runner → TimeoutExpired → main() tracebacks → all in-memory results for the
  run are lost (the write at line 363 never executes), including earlier fixtures'
  results in that run. (check.sh itself is `set -euo pipefail` and has no per-test
  timeout.)
- impact: a single pathological model patch nukes a whole multi-hour bench sweep's
  scoreboard; the operator sees a Python traceback instead of a results file, and the
  loop's "never fake a pass" doctrine is intact but its data is silently destroyed.
- suggested_fix: wrap the per-fixture verify in try/except TimeoutExpired → record
  `tests_passed:0, detail:"check.sh timed out"` and continue; also write results.json
  incrementally (per fixture) or at least in a finally block. Regression test: a fake
  check.sh that hangs, assert main() completes with a timeout row and exit 0.
- effort: S

## FINDING 5
- area: stale-artifact guard (nine/runtime/workflows.py) + gates/evidence.py
- severity: medium
- title: The stale guard exempts subdir paths and DIRECTORY artifacts — review_multi's
  `reviews/*.md` and build-multi's `solution/` can certify STALE attempt-1 content at
  SHIP with zero provenance (the T10-F2 fix is top-level-files-only)
- evidence: `workflows.py:512-521` — `if "/" in expected_name or os.sep in expected_name:
  continue` and `if p_expected.is_dir(): continue`. The manifest only ever registers
  top-level FILES (`job_dir.iterdir()` is non-recursive, `workflows.py:422`), so subdir
  and directory required artifacts have NO per-attempt provenance anywhere. Live
  instances: `review_multi_wf.py:38-39` `_DIM_FILES = ["reviews/security.md",
  "reviews/bugs.md", "reviews/quality.md", "reviews/arch.md"]` (each is a required
  artifact check), and `build_multi_wf.py:156` `required_artifact_check(["solution",
  "EVAL.json"])` where `solution` is a directory. Repro (hermetic, `/tmp/reproF.py` —
  real gate + verbatim guard logic; EVAL.json is the only file registered this attempt,
  `reviews/security.md` + `solution/` are attempt-1 leftovers):
  ```
  gate verdict: SHIP
  guard SKIPS 'reviews/security.md' (subdir exemption)
  guard SKIPS 'solution' (is_dir exemption)
  stale list: []
  => SHIP stands: gate certified STALE attempt-1 'reviews/security.md' and 'solution/' with zero provenance
  ```
  This is the exact T7-F1/T10-F2 lie class, still open for the subdir/dir shapes that
  two production workflows actually gate on.
- impact: a FIX-loop rerun where the reviewer/merger or build agent stops rewriting
  `reviews/*.md` or `solution/` still SHIPs, certifying attempt-1 files that are absent
  from the shipped manifest — the "manifest = this attempt's artifacts" promise is
  false for these lanes.
- suggested_fix: extend provenance to the shapes the manifest can track: either record
  subdir files in the manifest (recursive walk with relative paths) or make the stale
  guard require `.expected` subdir/dir entries to be re-registered this attempt
  (e.g. track the newest mtime of any file under the dir vs the attempt start);
  BLOCK otherwise. Regression test: FIX-rerun where the reviewer node skips
  `reviews/security.md` → gate must BLOCK, not SHIP.
- effort: M

## FINDING 6
- area: bench scoreboard (bench_nine.py main / TRACKER)
- severity: medium
- title: The bench always exits 0 and silently overwrites results.json — a 0/9 SHIP run
  is tool-level indistinguishable from 9/9, and the loop's "pass rate must never go
  down" rule has no automated enforcement or archive
- evidence: `bench_nine.py` `main()` returns 0 unconditionally (`bench_nine.py:371`);
  `results.json` + `results-{TASK_MODE}.json` are written with plain `write_text`
  (`bench_nine.py:363-364`), overwriting the previous run's file with no archive
  (archiving only happens if the operator remembers to set NINE_BENCH_RUNID — the
  documented default invocation does not), no comparison, no delta, no failure signal.
  docs/IMPROVEMENT_LOOP.md step 3 says "The pass rate must never go DOWN... if it
  regressed... fix" — but the tool provides no way to detect it: the comparison source
  is destroyed by the very run that would show the regression. Repro (hermetic,
  `/tmp/reproE.py` — BENCH_ROOT pointed at a temp dir, run_submit/verify stubbed to
  BLOCK):
  ```
  bench main() exit code: 0
  results rows: [(bugfix-small-001, BLOCK), (bugfix-small-002, BLOCK), ... 9 BLOCKs]
  results.json exists: True | results-full.json exists: True
  ```
  9/9 BLOCK and 9/9 SHIP both exit 0 and both overwrite the same files.
- impact: automation wrapping the bench (the heartbeat loop, CI, the runbook) cannot
  fail on regression; a silent 9/9→0/9 collapse (e.g. FINDING 1's stale-dir scenario)
  is recorded as a normal run and only a human diffing git history would notice.
- suggested_fix: add a regression comparison: read the previous results.json (or a
  per-RUNID archive) before overwriting, exit non-zero when the SHIP count or
  tests_total drops, and archive each run as `results-<RUNID>-<ts>.json` automatically.
  Regression test: run main() twice with stubs (9/9 then 0/9) and assert the second
  run exits non-zero with a regression message.
- effort: S

## FINDING 7
- area: convert_to_pytest (bench_nine.py) / bench scoring integrity
- severity: medium
- title: convert_to_pytest only converts TOP-LEVEL `test(...)` calls — looped or
  conditional tests are silently dropped (or raise, in which case the fixture is doomed
  to BLOCK with only a `[warn]`), and `expected_tests` is never cross-checked against
  check.sh's real count
- evidence: `bench_nine.py:117-143` — conversion walks only `tree.body` top-level
  `ast.Expr`/`Call` nodes (`bench_nine.py:120-124`); any `test()`/`test_raises()` call
  inside a `for`/`if`/helper is invisible. Two failure modes, both repro'd
  (hermetic, `/tmp/reproG.py` + `/tmp/reproG2.py`):
  1. all tests inside a loop → `RuntimeError("no test(...) calls found in runner")`
     (`bench_nine.py:127`) → bench prints `[warn] test conversion failed ... continuing
     with check.sh only` (`bench_nine.py:297`) and the fixture runs WITHOUT
     test_solution.py → debug verify hits the "no test evidence" branch → FIX×2 → BLOCK
     forever, even for a correct patch; `expected_tests: null` is the only trace.
  2. mixed top-level + looped tests → looped tests are DROPPED SILENTLY:
     ```
     runner had 5 test() calls; only 2 converted (40%). NO error raised - silent drop.
     expected_tests (bench records) = 2 but check.sh runs 5.
     ```
     The debug lane then SHIPs on a 2-test subset while the independent check.sh runs 5
     — and `expected_tests` (`bench_nine.py:295`) is never compared with the check.sh
     `tests_total` recorded in the same row, so the subset SHIP is invisible.
- impact: the convert machinery is the bridge between the fixture's own tests and the
  debug lane's real-test fix loop. Silent drops mean the gate certifies weaker evidence
  than the fixture defines; the loud variant turns any loop-based fixture into a
  permanent BLOCK with a one-line warning that does not explain the mechanism.
- suggested_fix: walk the AST recursively (ast.walk) for test/test_raises calls so
  looped/conditional tests convert (wrap them in a function body), or fail LOUD when
  the count of converted tests != the count of test()/test_raises() calls in the
  runner; add a parity assertion `expected_tests == tests_total` (or a loud mismatch
  field) in each results row. Regression test: a runner with 2 top-level + 3 looped
  tests → assert 5 converted defs and an equal count in `--collect-only`.
- effort: M

## FINDING 8
- area: bench scoring (bench_nine.py) + scoreboard semantics
- severity: medium
- title: BLOCKed fixtures are scored against the SEEDED BROKEN STARTER — the
  scoreboard's tests column inflates failures with the known-broken starter's partial
  score (7/12, 2/9) and misleads the loop's "pick the worst fixture" heuristic
- evidence: `bench_nine.py:320-327` — when the model never produced `patch.py` (BLOCK),
  the candidate falls back to `job_dir/"solution.py"`, which is the bench's own seeded
  STARTER (known-broken by fixture design). `verify_with_check_sh` then runs the
  fixture's check.sh against the starter. Repro (hermetic, `/tmp/reproL.py` — real
  check.sh vs the starter files):
  ```
  bugfix-small-009 STARTER score (what a BLOCKed fixture reports): 7 / 12 exit 1
  bugfix-small-002 STARTER score (what a BLOCKed fixture reports): 2 / 9  exit 1
  ```
  So a fixture whose debug lane BLOCKed (no patch at all) is recorded as `tests=7/12`
  or `2/9` — indistinguishable at the scoreboard level from "the model produced a
  partially-working patch". `candidate_unchanged_from_starter` is also True here, which
  the loop treats as a signal, but the row does not say the score IS the starter's.
- impact: the scoreboard (TRACKER/state.json "worst fixture" selection,
  IMPROVEMENT_LOOP.md step 4) ranks fixtures by tests_passed/total — BLOCKed fixtures
  appear mid-table instead of bottom, so the loop spends its next bench cycle on the
  wrong fixture and the score overstates model capability on BLOCKed lanes.
- suggested_fix: only score the candidate when it was run-PRODUCED this attempt; for
  BLOCK/no-patch rows record `tests_passed:0, tests_total:0, detail:"no patch produced"`,
  or add an explicit `scored_candidate:"starter"` field and make the scoreboard
  heuristic ignore starter scores. Regression test: stub run_submit to BLOCK, assert the
  results row reports 0/0 with a `candidate_file: "solution.py (starter fallback)"`
  marker instead of the starter's check.sh score.
- effort: S

## BONUS OBSERVATION (folded into FINDING 4's family, no separate ticket)
- area: bench timeout path (bench_nine.py `run_submit`)
- severity: low
- The per-fixture timeout does `proc.kill()` (`bench_nine.py:207`) on the direct nine
  child only. nine's bash verify node runs in a DETACHED session by design (T8-F5,
  `start_new_session=True`), so an in-flight pytest at timeout time survives the kill.
  Repro (hermetic, `/tmp/reproI5.py` — nine-side parent → detached bash-node →
  pytest-child simulation): after killing the parent (rc -9) the pytest child keeps
  running and writes a ghost file into the abandoned job dir (marker exists = True,
  pid still alive 3 s later). The bench then proceeds to the next fixture while a
  ghost writer holds the old job dir — the same ghost-file class recover wipes,
  except the bench never wipes. Suggested fix: kill the process group
  (`os.killpg(os.getpgid(proc.pid), SIGKILL)`) on timeout, mirroring T8-F5.
