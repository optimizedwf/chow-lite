# The Nine Improvement Loop (Torture Doctrine)

> "Continuous, non-stop, torturous improvement." — Adam
> Every cycle ships *something* real, or documents exactly why it could not.

The loop is a recurring agent cycle (RLM heartbeat, ~6h) that drives nine
toward a relentlessly rising benchmark score. It borrows the Chow system's
core doctrines: **evidence before SHIP**, small end-to-end automations,
deterministic routing, and a visible scoreboard (TRACKER).

## The scoreboard

- `bench/TRACKER.md` — human-readable run history (date, HEAD, bench score,
  what changed, verdict).
- `bench/state.json` — machine-readable state the loop MUST read/update every
  cycle: `{last_cycle, cycles_run, last_bench, bench_runs, est_quota_left,
  cooldown_until, last_score, best_score, next_target}`.
- `bench/runs/results.json` — latest full bench result (written by bench_nine.py).

## Cycle types

Each heartbeat picks ONE cycle type, in priority order:

### BENCH cycle (uses the real Gemini key — QUOTA-AWARE)
1. Read `bench/state.json`. A BENCH cycle is only allowed when
   `est_quota_left >= 30` AND `now > cooldown_until`. Otherwise → HARDEN.
2. Run `python bench/bench_nine.py` (all fixtures) OR a rotating subset via
   `NINE_BENCH_FIXTURES=bugfix-small-00X,...` — prefer the full set when quota
   allows; otherwise the worst N fixtures from the last run.
3. Parse `bench/runs/results.json`. **The pass rate must never go DOWN.** If
   it regressed since the previous run, the top priority for the FIX phase is
   to identify and revert/fix the regression.
4. Pick the WORST fixture (lowest tests_passed/total; prefer verdict FIX over
   SHIP-with-failed-tests; prefer `candidate_unchanged_from_starter=true`).
   Diagnose WHY it fails (read the job dir under `bench/runs/<fx>/run-*`).
5. FIX phase: implement the smallest real fix in nine (workflow node, gate,
   runtime, router, truncation, error handling). Write hermetic regression
   test(s) proving the fix. Never hack the bench harness to fake a pass —
   that is the #1 unforgivable sin.
6. GATES (non-negotiable, in this order):
   - `python -m pytest tests/ -q` → ALL pass (skips allowed only for
     key-gated live tests)
   - `ruff check nine/ tests/` → clean
   - `mypy nine/` → no NEW errors (2 pre-existing: schema_validation.py
     jsonschema stubs, memory/datahub.py datahub_agent_context)
7. Update `bench/TRACKER.md` + `bench/state.json` (est_quota_left -= 30 per
   full bench, -= 8 per single-fixture bench; set cooldown_until = +48h full /
   +12h single).
8. Commit + push. Commit message: `slice N: bench cycle <date> - fix <fixture>
   (<reason>)` or `slice N: bench cycle <date> - no fix needed (already green)`.

### HARDEN cycle (NO model calls — quota-free, do every other cycle)
Pick ONE, rotating:
1. **Static gap-hunt**: take every README/doc claim and verify it against the
   code (this is how the bench's 7 gaps were found: silent empty streams,
   exit-code-only self-tests, over-strict gates, truncation, misleading eval
   counts, keyword-only router). Fix what you find + tests + gates + commit.
2. **Test armor**: add hermetic regression tests for untested paths (grep for
   `def ` in nine/ with no test coverage; fuzz the gate/EvalJson parser;
   test the FIX-loop edge cases).
3. **Gate tightening**: bump a ruff rule (e.g. add `BLE`, `S`, `SIM`), raise
   mypy strictness, add a coverage floor (`pytest --cov`), add a pre-commit
   hook — anything that makes the next slice harder to ship broken.
4. **Torture harvest (simulated users)**: spawn 1-2 TORTURE-TESTER workers on
   `opencode-go/deepseek-v4-flash` (fallback `rue/cbcn/deepseek-v4-flash`) per
   `bench/torture/README.md`; collect `bench/torture/reports/*.md`, triage,
   implement the best finding with a regression test, log to
   `bench/torture/LEDGER.md`. Zero Gemini quota — this is the every-cycle default
   when no other HARDEN item is urgent.
4b. **New fixtures**: write `bugfix-small-00X+` (task.md, expected-behavior.md,
   starter/, tests/check.sh, rubric.json) modeled on the existing ones —
   each new fixture is a new torture instrument.
5. **Doc/UX truth**: fix CLI help text, error messages, README claims so the
   system says what it does and does what it says.

## Quota ledger (the torturous part is SUSTAINABILITY)

Gemini free tier: ~20 req/day, 5 req/min. A full bench ≈ 30-40 requests
(router 1 + diagnose 1 + patch 1 + verify bash 0, × fix-loop attempts × 5
fixtures). Rules:
- Never run a full bench more than once per 48h.
- Single-fixture benches (worst fixture) are the default smoke; ≤ 3/day.
- When `est_quota_left < 30` → HARDEN cycles until cooldown.
- 429s now fail LOUD (fix A): if the bench shows cli_error with 429 /
  RESOURCE_EXHAUSTED, log it, update quota to 0, switch to HARDEN.
- Never print the key value. Reference `~/.agent-vault/keys/gemini.key` only.

## Failure resilience

- If a cycle crashes (tool error, git conflict, quota), log the failure in
  TRACKER.md, set state.json `last_cycle` + `last_error`, and end the cycle.
  The next heartbeat retries. The loop NEVER dies — it just logs and waits.
- A blocked job, FIX verdict, or loud failure in the bench is DATA, not an
  error: it names the next fix.

## Definition of done for every cycle

1. bench/state.json updated (last_cycle, cycles_run++, score, quota).
2. bench/TRACKER.md updated (one row).
3. At least one commit pushed to optimizedwf/nine main (or a documented
   blocker row in TRACKER.md).
4. Git tree clean at end of cycle.
