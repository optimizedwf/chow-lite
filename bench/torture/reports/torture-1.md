# TORTURE-TESTER-1 report — runtime + gates attack surface

Adversarial QA of `nine` (model-driven workflow agent-OS) at /Users/adam26/chow-work/chow-lite.
All findings evidence-first, reproduced hermetically via `.venv/bin/python` from repo root (no network, no API key, no repo writes).

---

## FINDING 1
- area: runtime
- severity: high
- title: Unparsable model-routing output silently reroutes tasks to `respond` and stamps the decision `model=gemini-3.6-flash` (a routing metadata lie)
- evidence: nine/router/classifier.py:165-166 — `GeminiRouter.classify` returns `("respond", 0.0, "model output unparsable: ...")` on ANY JSON parse failure instead of signalling "no decision"; nine/router/classifier.py:204-219 — `Router.classify` then treats that as a valid model decision because `respond` IS in the catalog, so the keyword fallback at line 217 never runs and `model_used` stays `"gemini-3.6-flash"` (line 207). Repro (hermetic, fake model): `Router` with `GeminiRouter(FakeModel("{\"workflow_id\": \"build\", ...garbage"), workflows)`, `classify("please build a calculator")` → `workflow_id=respond, model=gemini-3.6-flash, confidence=0.0, reason="model output unparsable: ..."`. A control with an *invented* workflow id correctly falls back to keywords — only the unparsable case is broken.
- impact: Any flaky/truncated Gemini response (free-tier 429s, markdown fences, tool-call wrappers) routes "build X"/"test Y" tasks to the chat `respond` lane instead of the right workflow — misrouted work, wasted Gemini quota (respond + summarizer calls), and the RouteDecision + LEARN-loop route event record `model=gemini-3.6-flash` for a routing decision the model never actually produced. This is exactly the "eval metadata would lie about which lane served the job" failure the router docstring claims to prevent (nine/cli.py `_routing_model` comment).
- suggested_fix: Make `GeminiRouter.classify` raise or return `None` on unparsable output (or have `Router.classify` treat reason startswith "model output unparsable" as no-decision), so control falls through to `self.keyword.classify(...)` at line 217-219 with `model_used="deterministic-keyword"`. Regression test: stub model returning non-JSON text → assert `workflow_id == "build"` and `model == "deterministic-keyword"`.
- effort: S

## FINDING 2
- area: gates
- severity: high
- title: Build self-test fallback treats `python3 solution.py` exit 0 as proof — a do-nothing solution SHIPs as "verified"
- evidence: nine/chains/flagship.py:161-168 — `_build_self_test_command` fallback branch (no `test_solution.py` present, which is the default single `nine submit "build X"` path): `python3 -B solution.py > build.log 2>&1; rc=$?; if [ $rc -eq 0 ]; then printf '{"checks":[{"name":"solution-runs","passed":true,"message":"exit 0"}],"exit_code":0}' > EVAL.json`. Repro: `solution.py = "print('hello world')  # does NOT solve the task"` → EVAL.json `{"solution-runs": true, "exit_code": 0}`; gate (eval-json + exit-codes + artifacts, build_hop gate_checks at flagship.py:184-194) → `SHIP | all evidence checks passed`. Directly contradicts the gate doctrine "An exit code is not success" (nine/gates/evidence.py module docstring).
- impact: Any solution that merely *runs* without crashing — including a stub that does none of the task — SHIPs as verified work, then the review hop rubber-stamps it and the flagship chain ends SHIPPED. Broken/empty implementations ship with a self-attested EVAL.json.
- suggested_fix: The fallback must not certify functionality. Require a real pytest run (solution/test_main.py) or emit `passed:false` with message "no test evidence — runs but unverified" so the gate FIX-loops or BLOCKs. Regression test: build hop with a `print("hi")` solution → verdict must not be SHIP.
- effort: M

## FINDING 3
- area: gates
- severity: high
- title: Review hop is theater: review.md hardcodes "Verdict: PASS" and claims evidence it never reads; the gate only checks the file exists
- evidence: nine/chains/flagship.py:199-218 — `review_hop` is a bash node that unconditionally writes `"echo 'Verdict: PASS' >> review.md; echo 'Evidence: EVAL.json all checks passed, self-test exited 0' >> review.md"` (lines 205-206) with NO read of EVAL.json; `gate_checks` are only `required_artifact_check(["review.md"])` + `exit-codes_check()` (213-216). Repro: job dir with `EVAL.json = {"checks":[{"name":"tests-pass","passed":false,"message":"3 test(s) failed, 0 passed"}],"exit_code":1}` → run the review node command → review.md still says "Verdict: PASS / Evidence: EVAL.json all checks passed, self-test exited 0"; gate → `SHIP`.
- impact: The QA hop fabricates its verdict and its evidence citation; it can never block anything. In the flagship chain, a build that passed only via Finding 2's weak self-test is then "reviewed PASS" with a false evidence claim in review.md — a lie in the artifact that ships to the user.
- suggested_fix: Make the review node parse EVAL.json (e.g. grep for `"passed":false` / `exit_code`) and write the actual verdict; add a gate check that review.md's verdict is derived from EVAL.json's pass state (like `_review_verdict_check` but reading EVAL.json). Regression test: failing EVAL.json + review.md → review gate must FIX/BLOCK.
- effort: S

## FINDING 4
- area: gates
- severity: high
- title: review-multi gate SHIPs when every reviewer explicitly says "Verdict: FAIL"
- evidence: nine/workflows/review_multi_wf.py:40-48 — `_review_verdict_check` returns True if the substring `"Verdict:"` appears anywhere in REVIEW.md; it never inspects PASS vs FAIL (docstring at line 41 claims "PASS or FAIL"). Repro: `REVIEW.md` = "## Verdict: FAIL — sql injection everywhere" plus four dimension reviews each "## Verdict: FAIL" → `review_multi_hop` gate → `SHIP` (verdict: SHIP).
- impact: The QA lane certifies code that its own reviewer flagged FAIL; the job SHIPs with a failing review and the EVAL/verdict record claims a passing review. Ship-broken-work via a *silently passing* gate check.
- suggested_fix: Require the verdict line to match PASS, e.g. regex `^\s*#+\s*(Overall )?Verdict:\s*PASS\b` (and optionally require each dimension file to carry PASS). Regression test: REVIEW.md "Verdict: FAIL" → gate must not SHIP.
- effort: S

## FINDING 5
- area: runtime
- severity: medium
- title: Chain artifact manifest duplicates and misattributes every file across hops (17 manifest entries for 7 real files)
- evidence: nine/runtime/workflows.py:249-275 — after EVERY node the executor re-scans the *whole shared job dir* and registers any file it hasn't seen with `produced_by = nid` (the currently-running node), including files written by other hops and the seeded `task.txt`/`inbox.txt`; nine/chains/chain.py:239 rolls each hop's re-scanned manifest up into the chain job. Repro: deterministic demo lane (`inbox-triage-task-report`, 3 hops, no API key) → chain job artifacts = 17 entries for 7 distinct files; `triage.md` is claimed produced by `task` and `report`; `task.txt` by all three hops.
- impact: Artifact provenance lies: `nine artifacts <job_id>`, the ledger manifest, and the MemoryGraph (`_save_memory`, nine/chains/chain.py:231-240) record wrong producers and N× duplicates; chain stats/sizes inflate; LEARN/memory summaries get attributed to the wrong hop.
- suggested_fix: Snapshot the job dir before/after each node (or have nodes declare artifact paths) so only files actually written by the current node are registered; dedupe by (name, sha256) when rolling up in ChainExecutor. Regression test: 2-hop chain → manifest contains each (name, sha256) exactly once with the correct producing hop.
- effort: M

## FINDING 6
- area: runtime
- severity: medium
- title: `nine submit` and POST /v1/submit report BLOCK/FIX verdicts as success (exit 0 / HTTP 200)
- evidence: nine/cli.py:180-245 — `cmd_submit` always ends `return 0` (line 245) regardless of the final verdict, while the chain path returns 2 on non-SHIPPED (nine/cli.py:213); deploy/server.py `submit()` returns HTTP 200 with a `blocked` body. Repro: workflow with an empty gate (no checks → `BLOCK, "no evidence checks registered"`), `nine submit` → job status `blocked`, process exit code `0`.
- impact: CI/scripts that gate on exit code treat unverified, blocked jobs as success; the CLI's own docstring ("Exit codes: 0 ok, 1 error. An exit code is NOT task success") plus the inconsistent `nine chain` contract make the transport a silent lie for automation. Blocks and FIXes vanish from failure signals.
- suggested_fix: Return non-zero (e.g. 2, mirroring cmd_chain line 213) when the verdict is not SHIP; server should surface a 502/409 or explicit non-200 for blocked/failed jobs. Regression test: BLOCK verdict → exit code != 0.
- effort: S

## FINDING 7
- area: runtime
- severity: medium
- title: `nine recover` never re-executes: it stickers the job "recovered" and leaves it there (docs promise re-execution)
- evidence: nine/ledger/ledger.py:194-200 — `recover()` only transitions `blocked|failed -> recovered`; no code path issues `running` (LEGAL_TRANSITIONS["recovered"] = {"running","cancelled"}, ledger.py:33) and no executor is invoked; nine/cli.py:305 `cmd_recover` help says "recover a blocked/failed job". Repro: blocked job → `recover()` via a fresh ledger → durable status `recovered`, `attempts=0`, `verdicts=0`; CLI prints `recovered <id> -> recovered`.
- impact: Operators/automation believe recovery re-runs the job; instead the job is parked in a dead-end status, and a dashboard filtering on `blocked` now shows it as "recovered" — unverified work looks handled. Contradicts the docstring "BLOCKED/FAILED -> recovered -> running (re-execution)" (ledger.py:195).
- suggested_fix: Either remove the misleading help/docstring, or make `recover()` actually re-execute (transition to `running` and hand the job back to the executor). Regression test: recover a blocked job → a new execution attempt occurs (attempts/verdicts increment).
- effort: S

## FINDING 8
- area: runtime
- severity: high
- title: Research lane fabricates findings: the `research` keyword SHIPs canned text that ignores the task
- evidence: nine/chains/flagship.py:16-29 — the `research` node is a bash echo that writes `research.md` with a fixed line `"Key insight: evidence-gated execution keeps agents honest."` (line 27), echoing only the first 5 lines of task.txt; nine/registry.py:187 routes keywords "research", "investigate", "find out", "study" to this hop; gate_checks (flagship.py:42-47) only require research.md + HANDOFF.md to exist. Repro: task "research the economic impact of AI on small farms in the US" → research.md contains the canned insight, nothing about the task; gate → SHIP (HANDOFF.md present).
- impact: One of the most-routed lanes returns fabricated research in an evidence-gated system whose doctrine is "NEVER fabricated output" (responder.py, adk_runtime.py, gemma.py all repeat this). Downstream plan/build hops consume fake findings; users receive a "verified" document that answers nothing. It also wastes Gemini quota (the summarizer still distills the canned text).
- suggested_fix: Make the research hop a real model node (as research-quick/research-deep already are) writing task-derived findings, or remove it from `_BASE_KEYWORDS` and route "research" to research-quick/research-deep. Regression test: keyword "research X" must not produce the canned insight; research.md must contain task-derived content.
- effort: M
