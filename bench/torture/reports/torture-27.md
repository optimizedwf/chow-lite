# torture-27 — learn + memory subsystem (adversarial round)

- date: 2026-08-15 (cycle 24 complete; next_target gem-r2)
- target: learn + memory subsystem (`nine/learn/learner.py`, `nine/memory/graph.py`, `nine/memory/datahub.py`, `nine learn` CLI, chain-hop memory recording, route-event recording, redaction, corrupt-JSONL tolerance, candidate transitions, memory search, Firestore guards)
- mode: hermetic/static (zero Gemini quota) — source reads only, no live runs
- scope guard: LEDGER.md checked first; nothing below re-files a fixed finding

## Findings

### 1. (MEDIUM) recover re-run reuses the same route-event event_id — the LEARN loop is permanently blind to re-run observations

- evidence: `nine/cli.py:465` and `deploy/server.py:558` build the event id as `ev-{job.job_id[:8]}` — the id is a pure function of the JOB, not of the RUN.
- `nine recover` re-executes the same job id through the same `_execute_job` (cli.py:685), so the recovered run's outcome is recorded under the SAME event_id as the original run.
- `Learner.learn()` (`nine/learn/learner.py:240-244`) dedupes candidates with `used_events` = every event id that already seeded any candidate: the recovered run's SHIP/BLOCK/FIX observation is silently consumed by the original run's candidate evidence and NEVER produces its own candidate — even when the recovered run's verdict flips (BLOCK -> SHIP) or its confidence changes.
- repro (hermetic): submit a task that routes to a workflow with confidence 0.20 (a BLOCK/low-confidence event seeds a candidate, event_id `ev-<jobid8>`); then `nine recover <job>` after fixing the input. `nine learn scan` shows no second candidate and the events file has two lines with the SAME event_id — the re-run observation is invisible to the learner.
- why it matters: recover exists precisely to re-observe ("fresh evidence, same task" — cli.py docstring), and the LEARN loop's only input is route events. Two records, one dedupe slot => the improvement loop systematically under-learns from the exact runs that most need learning.
- fix direction: include a run nonce (attempt number, recover count, or uuid) in the event_id, or dedupe on (event_id, job_id, recorded_at).

### 2. (LOW/MEDIUM) empty/whitespace memory query returns the k most-recent records as "hits" (false positives)

- evidence: `nine/memory/graph.py:109` `terms = [t.lower() for t in query.split() if t]`; line 122 `if all(t in hay for t in terms)` — for `terms == []` the `all()` over an empty iterable is `True`, so EVERY scanned record matches.
- `LocalMemoryGraph.search_context` (graph.py:105-126) therefore returns the k most-recent records for any whitespace-only query; `FirestoreMemoryGraph.search_context` (graph.py:186-188) has the empty guard and returns `[]` — the two backends disagree.
- the CLI guard `nine/cli.py:111` `if not args.query:` catches only the empty string, not `"   "` (shell `nine memory search "  "` passes a whitespace string through), and the server path (`cmd_memory`-equivalent consumers) has no guard at all.
- repro: `nine memory search "   "` prints "k memory entries match '   '" with the newest entries — an operator/AI consumer believes the store is semantically matching their (empty) query.
- fix direction: early-return `[]` when `terms` is empty (Firestore already does), and reject whitespace-only queries at the CLI.

### 3. (LOW) FirestoreMemoryGraph.save_artifact_summary has no best-effort guard at all — the T22-F1 belt does not cover the cloud backend

- evidence: `nine/memory/graph.py:146-177` — the Firestore write is a bare `self._ref(memory_id).set({...})` with NO try/except, unlike `LocalMemoryGraph.save_artifact_summary` (graph.py:97-102) which wraps `OSError`.
- T22-F1 (LEDGER: FIXED slice-43) made aux-store writes best-effort, but the fix only touched the JSONL/local paths — Firestore is the production/Cloud Run backend named in the module docstring.
- caller belt: `nine/chains/chain.py:308` wraps `self._save_memory(...)` in `except OSError` only (line 260 is the hop route-event belt). `google.api_core.exceptions.*` (DeadlineExceeded, ServiceUnavailable, PermissionDenied, NotFound) are NOT `OSError` subclasses, so a Firestore outage/403 mid-hop raises UNCAUGHT through the chain executor (ChainError -> job failed) and 500s the server — exactly the "broken memory store must not fail the run" contract T22-F1 established for the local backend.
- repro (static): run any chain with `NINE_MEMORY=firestore` and a Firestore client that raises `google.api_core.exceptions.PermissionDenied` on `.set()`; the hop crashes with a raw traceback instead of a WARNING line.
- fix direction: wrap the `.set()` in `except Exception` (warn-and-continue, mirroring the local backend) or at least extend the chain.py belt to `Exception`.

### 4. (LOW) datahub MCP node reports `enabled: True` while doing no work (stub success)

- evidence: `nine/memory/datahub.py:40-41` — with `NINE_DATAHUB_MCP=1` + `datahub_agent_context` importable, the tool returns `{"enabled": True, "note": "...wire search/get_lineage here..."}` — an explicit TODO marker as a successful result.
- a workflow that consumes `datahub-context` output will certify the node as having contributed context when it contributed nothing (a silent no-op in the middle of an evidence-gated pipeline).
- repro: set `NINE_DATAHUB_MCP=1`, `pip install datahub-agent-context` (or stub importable), run any workflow with the datahub node; node output claims enabled with zero graph reads.
- fix direction: return `enabled: False` (with reason "not wired") until search/get_lineage is actually implemented, or gate the node's certification on real work.

## Verified-not-filed (already fixed / covered)

- corrupt-JSONL tolerance in events/candidates (T3-F4/T4-F1), non-UTF8 `errors='replace'` (T8-F7), redaction across submit paths (T4-F4 et al.), route-event CANCELLED skip (T16-F1/T18-F2), best-effort aux writes local path (T22-F1), Firestore ledger shape guards (T24-F2), junk-env warnings (T24-F5/T25-F3), submit LedgerError clean error (T25-F1).

## Method

- 24 cycles complete (state.json), last cycle 2026-08-15; next_target gem-r2 (bugfix-small-002/005/006/009/010).
- reads: LEDGER.md (full), TRACKER.md, learner.py (full), graph.py (full), datahub.py (full), chain.py (_save_memory), cli.py (_record_route_event, cmd_recover, cmd_learn, _apply/_revert, _regression_green, _git_commit), server.py (_record_route_event), schemas/route-event.schema.json, workflows.py (gate/manifest path).
