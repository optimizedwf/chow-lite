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

---
## Gap ledger (from bench baseline)
1. Empty ADK output passes silently (no retry, artifact never written) -> FIXED (slice 21)
2. Build self-test exit-code-only (unchanged buggy starter ships) -> FIXED (slice 21)
3. Debug gate requires ROOT_CAUSE.md even for perfect patches -> FIXED (slice 21)
4. Router keyword-substring only in CLI -> FIXED (slice 21: model-first when key present)
5. Task truncation 200/400/500 chars hides success criteria -> FIXED (slice 21: 1500)
6. Quota exhaustion looks like "agent did nothing" -> FIXED (slice 21: loud RuntimeError)
7. EVAL.json "0 failed, 0 passed" on pytest collection errors -> FIXED (slice 21)
