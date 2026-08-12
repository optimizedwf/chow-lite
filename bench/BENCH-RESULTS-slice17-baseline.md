# BENCH-RESULTS — nine vs chow-agent-evals bugfix-small fixtures

- **Date / host**: 2026-08-12, Dell `chow@100.111.182.5`
- **nine commit**: `c888ba6` (Slice 17, "analyze workflow")
- **Repo**: `~/chow-work/agent-comp/competitions/chow-lite` (venv via `uv sync --extra dev`)
- **CLI invocation used**: `./.venv/bin/nine submit --workdir <isolated-dir> --ledger <per-fixture-ledger> '<task from task.md>'` (entry point `nine.cli:main`, same as `python -m nine.cli`)
- **Model**: `gemini-3.6-flash` (hardcoded in the build/debug ADK lanes; `GEMINI_MODEL=gemini-3.6-flash` set in the bench env). API key loaded from `~/.agent-vault/keys/gemini.key` — path referenced only, value never printed or committed.
- **Runner**: `scripts/bench_nine.py` (new). Per fixture: isolated job dir → seeds fixture `starter/solution.py` + a `test_solution.py` converted 1:1 from the fixture's own `tests/check.sh` assertions → runs the real CLI → waits for SHIP/FIX/BLOCK → independently runs the fixture's own `tests/check.sh` against the produced fix file (`patch.py`, else `solution.py`). Per-run artifacts under `bench-runs/`.

## Method notes

- The fixture test suites are bash scripts (`tests/check.sh`), not pytest; the runner converts their embedded `test(...)`/`test_raises(...)` calls into pytest functions for the debug lane's verify node, and uses the original `check.sh` for the independent pass/fail count (9 tests per fixture).
- `nine submit` is synchronous; the runner seeds the job dir the instant it appears (the CLI creates it with a random UUID). The patch node and fix-loop re-runs re-read job-dir files, so late seeding is safe. In every run the seed landed before the verify node.

## Primary results — task = full `task.md` (the specified invocation)

| fixture | routed lane | verdict | final status | tests passed | attempts | time |
|---|---|---|---|---|---|---|
| bugfix-small-001 | build | SHIP | shipped | 9/9 | 1 | 11.8 s |
| bugfix-small-002 | debug | FIX | blocked | 2/9 | 3 | 30.3 s |
| bugfix-small-003 | debug | SHIP | shipped | 9/9 | 1 | 17.3 s |
| bugfix-small-004 | debug | SHIP | shipped | 9/9 | 2 | 16.3 s |
| bugfix-small-005 | debug | FIX | blocked | 5/9 | 3 | 3.3 s |

**Pass rate (verdict SHIP + full fixture suite green): 3 / 5 fixtures (60%)** — aggregate **34 / 45 tests (75.6%)**.

- 001 → build lane fixed `slice_list` correctly in one shot (`items[start:end+1]`, start clamp, empty-list safe): 9/9.
- 003 → debug lane SHIP in one attempt: correct `parse_int_list` via `patch.py`, 9/9.
- 004 → debug lane demonstrated the **fix loop**: attempt 1 FIX → attempt 2 SHIP, correct `merge_unique`, 9/9.
- 002, 005 → debug lane **blocked with no fix artifact**: the patch ADK agent never wrote `patch.py` (empty model output, see gaps). The 2/9 and 5/9 scores are the *buggy starter's own* scores (verified: starters pass exactly 2/9 and 5/9), i.e. no fix was delivered. 002's `ROOT_CAUSE.md` was written and was a correct, high-quality diagnosis.

## Secondary run — task = "## Task Description" section only (desc mode)

Routing was unchanged (see gaps #4): 001 → build, 002–005 → debug. Results corroborate the primary run:

| fixture | routed lane | verdict | final status | tests passed | attempts | time |
|---|---|---|---|---|---|---|
| bugfix-small-001 | build | SHIP | shipped | 3/9* | 1 | 1.5 s |
| bugfix-small-002 | debug | FIX | blocked | 2/9 | 3 | 12.6 s |
| bugfix-small-003 | debug | FIX | blocked | **9/9**† | 3 | 10.4 s |
| bugfix-small-004 | debug | FIX | blocked | 7/9 | 3 | 3.3 s |
| bugfix-small-005 | debug | FIX | blocked | 5/9 | 3 | 11.3 s |

\* 001's build agent returned an *empty* response; the gate SHIPPED the pre-seeded buggy `solution.py` (build self-test is exit-code-only — gap #2). 3/9 = buggy starter's own score.
† 003's `patch.py` was **correct (9/9 on the fixture suite)** yet the job BLOCKED: the diagnose agent never wrote `ROOT_CAUSE.md`, so the debug gate's required-artifact check failed every attempt (gap #3).

## Harness gaps found (precise, no workarounds applied)

1. **Empty ADK model outputs pass silently (biggest gap).** `ADKAgentNode` only retries on *exceptions*; when the agent returns an empty stream (no tool call, no text), the node "succeeds", no artifact is written, and the debug lane FIX-loops to BLOCK. This is why 002/005 never produced `patch.py` despite a correct `ROOT_CAUSE.md` (002).
2. **Build lane self-test is exit-code-only** (`python3 -B solution.py` → EVAL.json "exit 0"). It cannot tell a correct fix from an unmodified/buggy file, and the gate's required-artifact check is trivially satisfied by a pre-seeded `solution.py` in the job dir (the bench seeds the starter by design; the lane certified it unchanged when the agent returned empty).
3. **Debug gate requires `ROOT_CAUSE.md` even when the patch is perfect.** A correct `patch.py` that passes the full suite (003, desc run) is FIX-looped to BLOCK because the diagnose agent wrote nothing. The gate conflates "diagnosis missing" with "fix failing".
4. **Router is keyword-substring only in the CLI** (`build_default_router()` wires no model; the README's "Gemini router" isn't used by `nine submit`). Consequences seen: 001 routes to `build` because "implement" ⊂ "implementation" in the task text; any task containing the eval metadata "build: false" would also route to build; 002–005 route to `debug` via "fix the bug" (11 chars beats "build"/"error").
5. **Task truncation**: build lane truncates the task to 200 chars, debug to 500 — success criteria / edge cases beyond that are invisible to the model.
6. **Quota exhaustion looks like "agent did nothing"**: at ~40–60 requests the free-tier key hit `429 RESOURCE_EXHAUSTED` (confirmed with a direct `genai` probe). ADK's runner yields empty streams rather than raising, so the harness burns fix loops and records FIX/BLOCK instead of failing loud. Post-quota reruns (r1–r3) are all-empty and excluded from this report.
7. Minor: when pytest fails at collection (e.g. missing `patch.py`), EVAL.json reports "0 test(s) failed, 0 passed" with exit code 2 — a misleading evidence message (the FIX verdict itself is correct).

## Reproducibility

- Clean rerun: wait for the Gemini free-tier daily quota reset (the key at `~/.agent-vault/keys/gemini.key` was exhausted during this bench), then re-run `NINE_BENCH_TASK_MODE=full ./.venv/bin/python scripts/bench_nine.py` (optionally `NINE_BENCH_RUNID=rN` to keep per-run dirs).
- All per-run job dirs, ledgers, and results JSONs are under `bench-runs/` (`results-full.json`, `results-desc.json`); no secrets in any artifact (key value never written; verified no `AIza*` strings).
- Nothing was committed; working tree changes: `scripts/bench_nine.py` (new), `bench-runs/` (new, gitignored), `uv.lock` (from `uv sync`), `BENCH-RESULTS.md` (new), `bench-runs/` added to `.gitignore`.
