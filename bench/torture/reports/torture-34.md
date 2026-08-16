# Torture-Tester 34 — Round 17 Re-Attack (robustness + fixtures)

Worker: torture-34 (DS4 Flash fleet)
Surface: robustness + fixtures — env var handling, corrupt/bad JSON stores,
permission errors, missing files, Firestore parity, deploy/server error paths.
FRESH angles only; T-* findings in LEDGER (rounds 1-16) were cross-checked and
NOT re-filed.

Method: static source analysis + hermetic repros with `.venv/bin/python`
(system python lacks jsonschema). No real ADK/model calls (zero Gemini quota).

Checklist of invariants probed:
- every NINE_* env parse: junk -> warn + fallback, never crash
- every JSONL/JSON read path: corrupt line -> skip-or-warn, never raw-crash
- permission errors / missing files -> clean one-line error, never traceback
- Firestore/JSONL ledger parity
- /v1/* API error paths: proper status codes, no raw 500s, no secret leaks

---

## FINDING 1

- area: robustness (learn/events store read path)
- severity: high
- title: valid-JSON wrong-shape route-event lines raw-crash `nine learn events` AND `nine learn scan` (TypeError/ValueError, exit 1, full traceback)
- evidence: nine/learn/learner.py:107-115 — `RouteEventStore.all()` accepts ANY dict line via `RouteEvent(**rec)`; only `TypeError` (missing/unknown keys) is skipped. A hand-edited or version-skewed line with `confidence: "0.99"` (string) survives `all()` and crashes downstream:
  - `nine learn events` -> cli.py:723 `conf={ev.confidence:.2f}` -> `ValueError: Unknown format code 'f' for object of type 'str'` (repro: events.jsonl with `{"event_id":"ev-x-0","job_id":"j","task_redacted":"t","workflow_id":"debug","confidence":"0.99","router_version":"v","verdict":"SHIP","checks_passed":1,"checks_total":1,"fix_directive":"","recorded_at":"..."}` -> `nine --events <f> learn events`, exit 1, full traceback)
  - `nine learn scan` -> learner.py:281 `ev.confidence >= 0.7` -> `TypeError: '>=' not supported between instances of 'str' and 'float'` (same file, verdict=FIX)
- impact: one bad event line (the exact class of corruption T6-F3 hardened in the LEDGER for the ledger store, but never applied to the events store) bricks the whole LEARN loop — the improvement engine of the system — with a raw traceback. `nine learn scan` is also the quota-free automation the loop relies on.
- suggested_fix: give RouteEvent a strict-typed constructor path: validate `confidence` is a real number (isinstance float/int and finite), `checks_passed`/`checks_total` ints, `task_redacted`/`workflow_id`/`verdict` strings, in `RouteEventStore.all()` (and `__post_init__` for `learn()` construction), skipping non-conforming lines with the same WARNING convention used elsewhere. Regression test: seed the store with a string-confidence line + a bool-verdict line, assert `all()` skips both and `nine learn events`/`scan` exit 0.
- effort: S

---

## FINDING 2

- area: robustness (learn/candidates store)
- severity: medium
- title: `nine learn apply` raw-tracebacks when candidates.jsonl contains one valid-JSON wrong-shape line (update_status rewrite path not belt-complete)
- evidence: nine/learn/learner.py:180 — `CandidateStore.update_status` reads `self.path.read_text(...)` OUTSIDE the OSError try (the try only wraps the final write_text at 204-211). A read-only candidates.jsonl (chmod 000) or a directory at the path raw-tracebacks PermissionError/IsADirectoryError from cmd_learn apply/revert instead of a clean one-line error — the read-side belt (T28-F3) covers `all()`, and T30-F1 covered wrong-shape lines, but neither covers an OSError on the rewrite's READ.
- impact: ...
- suggested_fix: ...
- effort: S

---
