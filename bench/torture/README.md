# Torture-Tester Pipeline (simulated users)

Cheap-model workers (DeepSeek V4 Flash via opencode-go / rue tunnel) act as
adversarial users: they torture-test nine and file structured gap reports.
This simulates "people torture test + suggest improvements" at zero Gemini
quota cost and zero human cost.

## Fleet
- Model: `opencode-go/deepseek-v4-flash` (primary) / `rue/cbcn/deepseek-v4-flash` (fallback).
- One worker per attack surface; rotate surfaces each round:
  1. runtime + gates (adk_runtime, retry/empty-stream, EVAL gate parsing, self-test/verify, ledger)
  2. workflows + router + CLI + docs (truncation, bash quoting, plugin registry, router, error paths, README claims)
  3. robustness + fixtures (env handling, bad JSON, missing files, permission errors, new fixture ideas)

## Report contract
Workers write `bench/torture/reports/<worker>.md` (read-only otherwise):
per finding -> area / severity / title / evidence(file:line+repro) / impact /
suggested_fix(+regression-test idea) / effort(S|M|L).

## Triage (loop's job, not the worker's)
1. Harvest reports after each round; dedupe against TRACKER gap ledger + prior reports.
2. Implement the highest-value fix (critical/high, effort S/M) with hermetic regression tests.
3. Gates: pytest all-pass, ruff clean, mypy no new errors. Never fake a pass.
4. Log findings + fixes in TRACKER.md; keep a cumulative gap ledger in bench/torture/LEDGER.md.
5. Findings that are already fixed or out-of-scope get a LEDGER row saying so (evidence for the user).
