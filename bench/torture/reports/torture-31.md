# Torture-Tester 31 — Round 16 Re-Attack (runtime + gates)

Worker: torture-31 (DS4 Flash fleet, round-16 spawn 2026-08-16)
Surface: runtime + gates — re-attack AFTER round-15 harvest
(run_seq event ids both ledgers, gate-check error->BLOCK, events limit 1..1000,
XFF rate key, blank-task 422, Firestore _ref shape, NINE_GATE_TIMEOUT_S junk-env
warn, FIX-directive names failing checks, _cap_task_text). New angles: retry/
backoff edges, empty-stream handling, EVAL gate parsing, self-test/verify
strictness, ledger durability, timeout races, gate provenance/expected tags.
Method: static source analysis + hermetic repros (.venv/bin/python). No real
ADK/model calls (zero Gemini quota).

---

## Findings

_(populated incrementally)_
