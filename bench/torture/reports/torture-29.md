# Torture-Tester 29 — Round 15 Re-Attack (workflows + router + CLI + docs)

Worker: torture-29 (DS4 Flash fleet, round-15 spawn 2026-08-16)
Surface: workflows + router + CLI + docs — re-attack AFTER round-14 harvest
(run_seq event ids, gate-check error->BLOCK, events limit guard, XFF rate key,
blank-task 422, Firestore _ref shape).
Method: static source analysis + hermetic repros (.venv/bin/python). No real
ADK/model calls (zero Gemini quota).

Checklist of invariants probed:
- workflow registry: id collisions, plugin merge, keyword->id validity
- fix_directive construction: content, truthfulness, bleed across hops
- hop artifact manifest edge cases: namespacing, stale content, FIFO/device
- router fallback paths: model-unavailable behavior, confidence sanity
- CLI: --help/exit codes on every subcommand, clean errors, no raw tracebacks
- docs vs code: README/SUBMISSION claims must be true of code

---

## Findings

### F1 — [HIGH] plugin registry wrong-shape `PLUGIN_WORKFLOWS` bricks every nine command at import

- **Area**: workflows / plugin loader (nine/registry.py:254)
- **Severity**: high
- **Evidence**: `NINE_PLUGIN_REGISTRY` pointing at a syntactically valid module whose `PLUGIN_WORKFLOWS` is a non-dict (e.g. `= 42` or `= 'abc'`) raw-tracebacks at import: the `dict(getattr(mod, "PLUGIN_WORKFLOWS", {}))` conversion at registry.py:254 runs OUTSIDE the `try/except` that wraps `exec_module` (registry.py:245-249). Hermetic repro: rc=1 with `TypeError: 'int' object is not iterable` / `ValueError`, traceback at import of nine — EVERY command (`nine submit`, `nine --help`, `nine status`) dies. T4-F3 hardened the catalog loader but not this conversion step, which is the same "broken plugin config must not break nine" class.
- **Impact**: a single malformed-but-parseable plugin registry file takes down the whole CLI/server, not just the plugin lane.
- **Suggested fix**: wrap the `dict(...)` comprehension in the same try/except (or `isinstance` guard) as `exec_module`; warn loudly and return `{}` on wrong shape.
- **Effort**: S (5-line change + test).

### F2 — [HIGH] Firestore ledger `recover()` never bumps `run_seq` → re-run route events silently deduped

- **Area**: CLI/recover + LEARN (nine/ledger/firestore_ledger.py:181-197)
- **Severity**: high
- **Evidence**: `JSONLLedger.recover()` bumps `job.metadata["run_seq"]` (nine/ledger/ledger.py, torture-27 F1) so a re-run's route event id `ev-<jobid>-<seq>` differs and `Learner.learn()` sees both observations. `FirestoreLedger.recover()` (firestore_ledger.py:181) does NOT — it resets attempts and writes `status/updated_at/attempts` only, leaving `run_seq` at 0. The CLI/server event recorder builds `ev-{job_id[:8]}-{int((job.metadata or {}).get('run_seq', 0))}` (nine/cli.py `_record_route_event`), so a Firestore-backed recovery records the re-run under the ORIGINAL event id and `Learner.learn()` dedupes it away (learner.py: used_events) — LEARN is blind to the re-run exactly as T27-F1 fixed for the JSONL path.
- **Impact**: on Firestore deployments, a recovered run that flips BLOCK→SHIP (or vice versa) never seeds a candidate; the fix loop's core learning signal is silently lost.
- **Suggested fix**: mirror JSONLLedger: `job.metadata["run_seq"] = int(job.metadata.get("run_seq", 0)) + 1` before writing, and persist `metadata` (or run_seq) in the `_ref(job_id).update(...)` call.
- **Effort**: S.

### F3 — [MED] chain FIX directive drops the failing check names — flagship FIX retries are blind

- **Area**: chains (nine/chains/chain.py:281-286) vs workflows (nine/runtime/workflows.py:1041-1050)
- **Severity**: medium
- **Evidence**: the single-workflow FIX directive enumerates every failing check with its message: `"gate FIX after attempt N: <k>: <message>; ..."` (workflows.py:1042-1049). The chain hop FIX directive reduces it to `"hop <id> failed gate (attempt N): missing artifacts [...]"` or the generic `"gate checks failed"` (chain.py:281-282) — the actual `eval_results` failing check names/messages (`res["verdict"]["eval_results"]`) are available right there but dropped. Flagship ADK hops expose only a `write_file` tool (no read tool), so a FIX retry cannot even inspect the failing artifact — the directive is the only signal and it names nothing.
- **Impact**: chain hop FIX retries re-burn model budget on a byte-vague prompt; the T7-F2 fix (directive must "name what failed") is defeated in the chain path.
- **Suggested fix**: build the chain reason from `res["verdict"]["eval_results"]` exactly like workflows.py (join failing `k: message`), fall back to missing-artifact list only when no check failed.
- **Effort**: S.

### F4 — [MED] flagship truncations ignore `NINE_TASK_CAP` and slice silently (no ellipsis)

- **Area**: chains/flagship.py — research/plan/build `_run` (lines 50-51 and 6 occurrences of `[:1500]`)
- **Severity**: medium
- **Evidence**: flagship ADK hops hardcode `task[:1500]` and `fix_dir[:1500]` (flagship.py:50-51, same in research/plan/build), ignoring the documented `NINE_TASK_CAP` env cap that `debug_wf`'s `_env_cap("NINE_TASK_CAP")` (default 1400) honors. The slices also drop the tail with NO ellipsis marker, so the model cannot tell the task was truncated — a long task silently loses its ending (where acceptance criteria usually live). `debug_wf`'s `_cap_instruction` adds `... [truncated]`, the flagship nodes do not.
- **Impact**: env cap is a no-op for the flagship lanes; truncated prompts mislead the model about task completeness and can burn the first hop on a mangled task.
- **Suggested fix**: route all three hops through the same `_cap_instruction`/`_env_cap` helper; append an explicit truncation marker.
- **Effort**: S.

### F5 — [MED] `demo_lane` docstring says "4 deterministic hops" but the chain has 3

- **Area**: docs (nine/chains/flagship.py:549 `demo_lane` docstring)
- **Severity**: low
- **Evidence**: docstring: "This is deliberately small (4 deterministic hops)"; the actual chain is `hops=[triage, task, report]` — 3 hops (verified at flagship.py demo_lane definition).
- **Impact**: docs lie to humans and to any agent that reads source docstrings; a "4-hop demo" expectation breaks tutorial/README mental models.
- **Suggested fix**: change to "3 deterministic hops".
- **Effort**: S.

### F6 — [LOW] README badge/roadmap claim 568 passing (573 collected); actual collect = 578 tests

- **Area**: docs (README.md badges + Roadmap section)
- **Severity**: low
- **Evidence**: README: `tests-568 passing, 5 skipped (573 collected)`; hermetic `pytest tests/ --collect-only -q` = **578 tests collected in 0.23s** (rc 0). README also repeats "568 passing tests (573 collected, 5 live-gated skips)" in Roadmap.
- **Impact**: stale CI badge misleads contributors about the real suite size (578 ≠ 573); test-count drift suggests the badge is hand-maintained, not generated.
- **Suggested fix**: regenerate the badge/count from a real run (or add a test-count check to CI).
- **Effort**: S.

## Severity count
- HIGH: 2 (F1, F2)
- MED: 2 (F3, F4)
- LOW: 2 (F5, F6)
