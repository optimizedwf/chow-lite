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
| T3-F1 | 2026-08-13 | torture-3 | high | debug + build-multi verify SHIP stubs as 'verified' (exit-0/py_compile only, no tests) | FIXED 346a71a (no test evidence -> passed:false, FIX loop toward real tests) |
| T3-F2 | 2026-08-13 | torture-3 | high | Standalone review SHIPs fabricated 'Verdict: PASS' when EVAL.json missing (grep exit 2 -> else branch) | FIXED 346a71a (missing EVAL.json -> Verdict: FAIL + exit 1; review.md never cites evidence that never existed) |
| T3-F3 | 2026-08-13 | torture-3 | medium | recover on shipped/running/awaiting_evidence crashes with InvalidTransition traceback; recovered jobs keep burned attempts | FIXED 346a71a (dup of T4-F2: LedgerError unless blocked/failed, attempts reset to 0) |
| T3-F4 | 2026-08-13 | torture-3 | medium | One corrupt/partial ledger line bricks every command (_load crashes) | FIXED 346a71a (dup of T4-F1: skip + count corrupt lines; same for memory/learn stores) |
| T3-F5 | 2026-08-13 | torture-3 | medium | Node.timeout_seconds ignored for prompt/tool/subagent (bash-only) - hung model call leaves job running forever | FIXED 346a71a (daemon-thread deadline for callable nodes; fail loud on timeout) |
| T3-F6 | 2026-08-13 | torture-3 | medium | FIX loops re-run ALL nodes - failing gate re-burns full Gemini budget | NOT SHIPPED (soundness: node-replay is unsound for mutating bash nodes; model nodes must respond to fix_directive by design. Revisit: per-node replayable flag or chain-level hop caching) |
| T3-F7 | 2026-08-13 | torture-3 | medium | ADK write_file accepts ../ escapes (cross-job EVAL.json poisoning, catalog/ledger tampering) | FIXED 346a71a (_contained_write refuses targets outside job_dir, all 3 flagship sites) |
| T3-F8 | 2026-08-13 | torture-3 | low | redact() case-sensitive - API_KEY=/PASSWORD=/TOKEN: leak verbatim | FIXED 346a71a (re.IGNORECASE) |
| T4-F1 | 2026-08-13 | torture-4 | high | One corrupt ledger line bricks every nine command incl. submit (no CLI recovery) | FIXED 346a71a (defensive _load: skip bad lines, corrupt_lines count, LedgerError for OSError; memory/learn stores too) |
| T4-F2 | 2026-08-13 | torture-4 | high | recover on shipped/cancelled destroys shipped artifacts then crashes InvalidTransition while ledger claims shipped | FIXED 346a71a (LedgerError refusal BEFORE wipe; clean one-line error) |
| T4-F3 | 2026-08-13 | torture-4 | high | Corrupt catalog.json bricks every command at import time (load_catalog only caught FileNotFoundError) | FIXED 346a71a (JSONDecodeError/OSError/non-object -> stderr warning + base keywords) |
| T4-F4 | 2026-08-13 | torture-4 | high | Redaction applied on only ONE of three submit paths - chain + POST /v1/submit store raw secrets | FIXED 346a71a (redact at ledger boundary JSONLLedger.submit; all paths covered; execution keeps raw task) |
| T4-F5 | 2026-08-13 | torture-4 | medium | Whitespace GEMINI_API_KEY passes every key guard - jobs burn retries + confusing auth error | FIXED 346a71a (.strip() at all 6 guard sites; fails loud as documented) |
| T4-F6 | 2026-08-13 | torture-4 | medium | Global --ledger silently ignored by submit/chain (subparser redefinition clobbers) - sandbox jobs land in prod ledger | FIXED 346a71a (default=argparse.SUPPRESS + getattr fallbacks) |
| T4-F7 | 2026-08-13 | torture-4 | low | New bench fixtures bugfix-small-006/007/008 (strict-JSON output, empty/unicode input, missing-env fail-loud) | FIXED 2026-08-13 slice 25 (3 fixtures shipped: starter-broken negative control + fixed-candidate positive, 14 hermetic pytest tests incl. check.sh->pytest convert path; bench_nine default range 1..8) |
