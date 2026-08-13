# Nine Improvement Loop — TRACKER

Scoreboard for the continuous improvement loop. Baseline recovered from the
Dell bench run (slice 17, c888ba6, 2026-08-12). One row per cycle.

Legend: score = fixtures SHIP'd / total, tests = tests_passed/tests_total.
Cycle types: BENCH (real Gemini) / HARDEN (quota-free).

| date | HEAD | cycle | score | tests | notes |
|------|------|-------|-------|-------|-------|
| 2026-08-12 | c888ba6 (slice 17) | BENCH baseline (Dell) | 3/5 (60%) | 34/45 (75.6%) | 001 SHIP but candidate_unchanged (exit-code-only self-test); 002 FIX 2/9; 003 FIX 9/9 (perfect patch blocked: ROOT_CAUSE.md gate); 004 FIX 7/9; 005 FIX 5/9. 7 gaps logged -> slice 21 fixes A-F |
| 2026-08-12 | e48354b (slice 22) | HARDEN | — | 183 pass | doc-truth gap-hunt: README + workflows.py claimed 'Gemini 3.5 Flash' while code defaults gemini-3.6-flash everywhere -> docs now match code |
| 2026-08-12 | 0bfdbe7 (slice 22) | BENCH smoke | 0/1 (quota) | 2/9 | bugfix-small-002 via real key: ADK empty stream (quota exhausted by bench run) -> loud RuntimeError per fix A. Loop validated end-to-end locally. Switching to HARDEN until cooldown 2026-08-14 |
| 2026-08-12 | 920d3a9 (slice 21) | FIXES | — | 183 tests pass | bench-findings fixes shipped: loud empty-stream, pytest self-test, debug gate relaxed, 1500-char truncation, collection-error msgs, model-first router |

| 2026-08-13 | 449accc (slice 23) | TORTURE HARVEST 1 | — | 196 tests pass | 9/16 worker findings FIXED (router unparsable+word-boundary, review-multi FAIL, build-no-tests, review-from-EVAL, transform ext trust, submit exit 2, ledger redaction, cancel/recover clean); 4 deferred (manifest snapshot M, recover re-execute, research/plan ADK, summarize-empty), 1 partial (review done, research+plan next) |

| 2026-08-13 | e693917 (slice 23) | TORTURE HARVEST 2 | — | 204 tests pass | 6/6 deferred/partial findings FIXED: research+plan hops -> ADK (T2-F1/T1-F8), manifest per-attempt snapshot (T1-F5/T2-F4), recover re-executes (T1-F7), summarize-empty gate (T2-F8). ALL 16 worker findings FIXED (10 in harvest 1 @ 449accc, 6 in harvest 2 @ e693917) |

| 2026-08-13 | a72d43f (slice 24) | HARDEN (test armor) | — | 223 tests pass | EVAL-gate honesty: `"passed":"false"` (string) used to SHIP (truthy); only literal JSON true passes now; non-dict roots / non-object checks / unnamed failed checks fail closed with clear messages. 19 new gate armor tests. Spawned torture-3 (runtime+gates) + torture-4 (robustness+fixtures) on DS4 Flash for next harvest |

| 2026-08-13 | 346a71a (slice 24) | HARDEN (torture harvest 3) | — | 238 tests pass | torture-3+torture-4 (DS4 Flash round 2): 15 findings triaged, 11 FIXED + 2 dup (recover status, corrupt ledger) + 1 deferred w/ soundness note (FIX-loop caching) + 1 deferred (3 new fixtures). Highlights: redaction moved to ledger boundary (chain/server now covered), recover refuses non-blocked/failed cleanly, corrupt ledger/catalog degrade instead of bricking, whitespace key fails loud, review-of-nothing FAILs, debug/build-multi need real test evidence, node timeouts enforced, write_file contained |

| 2026-08-13 | slice 25 (fixtures) | HARDEN | — | 252 tests pass | T4-F7: bench fixtures bugfix-small-006 (strict-JSON render/validate), 007 (empty/whitespace/unicode title_case), 008 (missing-env fail-loud CLI) — each with starter-broken negative control, fixed-candidate positive, check.sh->pytest convert path verified; bench_nine.py default range extended 1..5 -> 1..8 |

| 2026-08-13 | 796658d (slice 25) | HARDEN (torture harvest 4) | — | 268 tests pass | torture-5 + torture-6 (DS4 Flash round 3): 16 findings - 15 FIXED + 1 PARTIAL (T6-F5 thread-kill impossible). Highlights: shared fsafety contained_write swept all 24 model write sites, demo keywords out of production routing, blocked chains reach terminal state, recover refuses missing task.txt, standalone plan can SHIP, whitespace-key guards swept (27 sites), NaN confidence rejected, symlinks never evidence, non-UTF8/garbage ledger lines skip, redact covers AWS/Slack/JSON-quoted/== shapes, --workdir parent parser, README + exit-code doc truth. Reports sanitized for GitHub push protection (secret-shaped test strings redacted). |

| 2026-08-13 | slice 26 (HARDEN) | TEST ARMOR | — | 287 tests pass | hermetic armor for the two lowest-coverage runtime modules: gemma_generate failure modes (no-key/no-call, requests-None, HTTP 429/500, empty candidates, no text parts, exception -> None; success URL/model/header/timeout) and ADKAgentNode fake-runner paths (empty stream -> loud RuntimeError, 3x raise surfaces error, transient retry then success, artifact write + function_calls, session dedupe per job, make_adk_node spec, register_adk_agents). 19 new tests; coverage 73% -> 74% (gemma 46%->100%, adk_runtime 64%->96%). Spawned torture round 4 (torture-7 chains+gates+plugins+server, torture-8 runtime edges+fixture proposals) on DS4 Flash; reports pending. |

| 2026-08-13 | 0745237 (slice 27) | HARDEN (torture harvest 5) | — | 309 tests pass | torture-7 (chains/gates/plugins/server) + torture-8 (runtime edges): 16 findings - 15 FIXED + 1 dup (T7-F7 == T8-F1). Highlights: symlinks NEVER evidence (manifest loop + snapshot + explicit-artifact; dangling-target test fixed), recover refuses symlinked job_dir, cancel is now HONEST (durable-ledger poll -> CANCELLED, no shipped line, chains abort between hops), callable timeouts retried via NodeTimeoutError + timeout_seconds>=1 validation, bash process-group kill on timeout, recover --force for stale running, learn stores + memory graph byte tolerance, doc sweep 3.5->3.6 Flash, stale-EVAL SHIP guard, flagship fix_directive, compose built-in id collision refusal, honest explicit-chain route decision, recover chain loud-fail, chunked-body 413 cap, hop_artifacts forwarding. 22 new hermetic tests. Fixtures 009-011 deferred. |

---
## Gap ledger (from bench baseline)
1. Empty ADK output passes silently (no retry, artifact never written) -> FIXED (slice 21)
2. Build self-test exit-code-only (unchanged buggy starter ships) -> FIXED (slice 21)
3. Debug gate requires ROOT_CAUSE.md even for perfect patches -> FIXED (slice 21)
4. Router keyword-substring only in CLI -> FIXED (slice 21: model-first when key present)
5. Task truncation 200/400/500 chars hides success criteria -> FIXED (slice 21: 1500)
6. Quota exhaustion looks like "agent did nothing" -> FIXED (slice 21: loud RuntimeError)
7. EVAL.json "0 failed, 0 passed" on pytest collection errors -> FIXED (slice 21)
