# Torture-Tester Gap Ledger

Cumulative ledger of worker findings + disposition (fixed / wontfix / duplicate).
One row per finding: id | date | worker | severity | title | disposition | slice/commit.

| id | date | worker | sev | title | disposition |
|----|------|--------|-----|-------|-------------|

| T1-F1 | 2026-08-13 | torture-1 | high | Unparsable model output reroutes to `respond` + stamps gemini route (metadata lie) | FIXED 449accc (classifier.py: parse failure = no decision -> keyword fallback, model_used honest) |
| T1-F2 | 2026-08-13 | torture-1 | high | Build self-test exit-0-only: stub solution.py SHIPs as verified | FIXED 449accc (no test_solution.py -> passed:false 'no test evidence'; builder must write tests) |
| T1-F3 | 2026-08-13 | torture-1 | high | Review hop is theater: hardcoded 'Verdict: PASS' + false evidence citation | FIXED 449accc (verdict derived from EVAL.json; consistency gate; standalone review writes own EVAL.json) |
| T1-F4 | 2026-08-13 | torture-1 | high | review-multi SHIPs when reviewers write 'Verdict: FAIL' | FIXED 449accc (gate requires PASS verdict line) |
| T1-F5 | 2026-08-13 | torture-1 | medium | Chain artifact manifest duplicates/misattributes files across hops | FIXED e693917 (per-attempt manifest snapshot: only files created/modified this attempt are registered) |
| T1-F6 | 2026-08-13 | torture-1 | medium | `nine submit`/POST report BLOCK/FIX as success (exit 0/HTTP 200) | FIXED 449accc (CLI exits 2 on non-SHIP; server 200 semantics on next pass) |
| T1-F7 | 2026-08-13 | torture-1 | medium | `nine recover` never re-executes (parks job in dead-end status) | FIXED e693917 (recover re-executes: stale artifacts wiped, task restored, shared _execute_job) |
| T1-F8 | 2026-08-13 | torture-1 | high | Research lane fabricates findings (canned insight, ignores task) | FIXED e693917 (research hop = real ADK LlmAgent, model-or-fail, nonempty gate) |
| T2-F1 | 2026-08-13 | torture-2 | critical | Flagship research/plan/review hops are canned bash stubs (review rubber-stamps) | FIXED e693917 (research+plan hops now real ADK LlmAgents; review was already derived-from-EVAL in 449accc) |
| T2-F2 | 2026-08-13 | torture-2 | high | Build self-test exit-0-only when no tests (stub SHIPs) | FIXED 449accc (duplicate of T1-F2) |
| T2-F3 | 2026-08-13 | torture-2 | high | Transform trusts model-written TARGET.txt (garbage OUTPUT.txt SHIPs) | FIXED 449accc (unsupported ext rejected; parseable formats only) |
| T2-F4 | 2026-08-13 | torture-2 | medium | FIX-loop reruns leave stale artifacts + wrong produced_by in shipped manifest | FIXED e693917 (snapshot filter stops FIX-rerun re-registration of untouched files) |
| T2-F5 | 2026-08-13 | torture-2 | high | Keyword router substring matching misroutes (latest->test, plant->plan) | FIXED 449accc (word-boundary regex) |
| T2-F6 | 2026-08-13 | torture-2 | medium | Raw task text incl. credentials stored unredacted in ledger | FIXED 449accc (redact() at ledger boundary + space-separated secret patterns) |
| T2-F7 | 2026-08-13 | torture-2 | low | cancel/recover unknown id -> raw Python traceback | FIXED 449accc (LedgerError -> clean one-line error, exit 1) |
| T2-F8 | 2026-08-13 | torture-2 | medium | summarize-standalone SHIPs 'summary of nothing' (empty workspace) | FIXED e693917 (source-present gate: empty workspace -> BLOCK, never SHIP) |
| S24-F1 | 2026-08-13 | loop self-hunt | high | EVAL gate SHIPs on `"passed": "false"` string (truthiness lie) | FIXED a72d43f (strict boolean contract: only literal JSON true passes; shape defenses for non-dict roots/non-object checks/unnamed failures) |
