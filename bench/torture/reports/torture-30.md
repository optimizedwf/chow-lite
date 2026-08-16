# Torture-Tester 30 — Round 15 Re-Attack (robustness + fixtures)

Worker: torture-30 (DS4 Flash fleet, round-15 spawn 2026-08-16)
Surface: robustness + fixtures — re-attack AFTER round-14 harvest
(corrupt JSON lines, permission-denied dirs, NINE_* env junk, concurrent
writes, unicode/emoji/huge tasks, unreadable memory files, bad server
config, PROPOSE bugfix-small-012+ specs).
Method: static source analysis + hermetic repros (.venv/bin/python). No real
ADK/model calls (zero Gemini quota).

Findings were verified by the harvest turn against the live code (each
repro re-run here); the worker's session transcript (sub-6a5dfc00, 166
messages) documents the discovery path and hermetic evidence.

---

## Findings

### F1 — [MED] `CandidateStore.update_status` raw-crashes on a wrong-shape JSON line

- **Area**: LEARN mutation path (nine/learn/learner.py, CandidateStore.update_status)
- **Severity**: medium
- **Evidence**: a candidates.jsonl containing one valid-JSON wrong-shape
  record (e.g. `"not-a-dict"` or `[1,2]`) makes `nine learn apply` /
  `nine learn revert` raw-traceback with
  `AttributeError: 'str' object has no attribute 'get'`. Hermetic repro
  (re-run here): `all()` skips the bad line via its `except TypeError`,
  but `update_status` rewrites the file calling `rec.get(...)` on any
  JSON-parseable line with NO shape guard. Same family as T14-F8 (catalog
  shape guard) and T6-F3 (ledger `_looks_like_job`) — the candidate store's
  WRITE path was never guarded (reads were, in T28-F3).
- **Impact**: one corrupt line bricks the whole LEARN mutation path
  (`apply`/`revert`) with a raw traceback; the operator cannot apply or
  roll back improvements.
- **Suggested fix**: in `update_status`, skip non-dict lines (or guard the
  rewrite with `isinstance(rec, dict)` + required-key check), same as the
  read paths.
- **Effort**: S.

### F2 — [LOW] malformed `NINE_GATE_TIMEOUT_S` silently falls back to 60 with no warning

- **Area**: env junk (nine/runtime/workflows.py `_gate_timeout_s`)
- **Severity**: low
- **Evidence**: `int(os.environ.get("NINE_GATE_TIMEOUT_S", "60"))` in a
  bare `except ValueError: timeout_s = 60` — NO stderr warning. The
  established junk-env convention (T9-F6, T22-F2, and the T24-F5 fix that
  made `NINE_MAX_LLM_CALLS=l4` warn loudly) requires malformed env values
  to surface once on stderr so a typo is not invisible to the operator.
- **Impact**: an operator who believes they tightened the gate window via
  `NINE_GATE_TIMEOUT_S=1200` gets the 60s default with zero signal when
  they typo it — exactly the T24-F5 failure class.
- **Suggested fix**: mirror the NINE_MAX_LLM_CALLS pattern — on ValueError,
  print a one-line warning to stderr before falling back.
- **Effort**: S.

### F3 — [MED] `nine memory search` raw-crashes when the memory path is a directory

- **Area**: memory (nine/memory/graph.py `LocalMemoryGraph.search_context` + nine/cli.py `cmd_memory`)
- **Severity**: medium
- **Evidence**: `search_context` does `open(self.path, ...)` with NO OSError
  belt — a directory (or unreadable FIFO) at the memory path raises
  `IsADirectoryError` straight out of `cmd_memory search`. T28-F3 put the
  read-side OSError belt on events/candidates stores, and T6-F8/`cmd_memory
  list` guards its own read — but `cmd_memory search` → `search_context()`
  is unguarded. Hermetic repro: `mkdir jobs/memory.jsonl; nine memory
  search foo` raw-tracebacks IsADirectoryError.
- **Impact**: same "unusable path must be ONE clean line" contract (T12-F8)
  violated on the memory search surface; server /v1/memory (if wired) and
  CLI both crash.
- **Suggested fix**: wrap the read in try/except OSError returning [] (+ a
  one-line warning), same as RouteEventStore.all().
- **Effort**: S.

## Severity count
- HIGH: 0
- MED: 2 (F1, F3)
- LOW: 1 (F2)
