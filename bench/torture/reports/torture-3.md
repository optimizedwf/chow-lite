# TORTURE-TESTER-3 report — runtime + gates + ledger attack surface

Adversarial QA of `nine` (model-driven workflow agent-OS) at /Users/adam26/chow-work/chow-lite.
Surface: adk_runtime node execution, EVAL.json gate parsing, build self-test/verify chain, ledger.
All repros hermetic (`/tmp` dirs, `.venv/bin/python` / `.venv/bin/nine` from repo root, no API key,
no repo writes). Verified against HEAD d6a237f (slice 24). 8 findings, sorted by severity.

---

## FINDING 1
- area: build self-test/verify chain
- severity: high
- title: debug + build-multi verify chains still certify "exit 0 / py_compile" as verification — a stub that prints nothing useful SHIPs with EVAL.json claiming "solution builds and verifies"
- evidence: nine/workflows/debug_wf.py:216-220 (`_build_verify_command` else branch: `python3 -B patch.py > build.log 2>&1; rc=$?` → on rc 0 writes `{"checks":[{"name":"patch-runs","passed":true,"message":"patch.py exit 0"}],"exit_code":0}`) and nine/workflows/build_multi_wf.py:109-116 (`elif [ -f solution/main.py ]` runs the entrypoint; else `python3 -B -m py_compile solution/*.py` → both write `{"checks":[{"name":"multi-build-verified","passed":true,"message":"solution builds and verifies"}],"exit_code":0}`). Repro (run in /tmp): job dir with only `patch.py = print("TODO: fix the bug")` and no test_solution.py → run the debug verify command → EVAL.json `patch-runs passed:true`; EvidenceGate with the debug hop's checks (eval_json_check + exit_codes_check + required_artifact_check(["patch.py","EVAL.json"])) → `VERDICT: SHIP | all evidence checks passed`. Same for build-multi: `solution/main.py = print('TODO: implement the whole project')` → EVAL.json `multi-build-verified passed:true "solution builds and verifies"` → SHIP. The flagship build hop (flagship.py:216-240) was fixed to fail "no test evidence", but these two lanes still SHIP an untested stub. Contradicts the gate doctrine "An exit code is not success" (nine/gates/evidence.py module docstring).
- impact: `nine submit "debug ..."` and `nine submit "build-multi ..."` SHIP completely unimplemented code as "verified" — EVAL.json claims the fix/project "builds and verifies" when nothing was tested. The flagship chain's build hop FIX-loops on the same stub, but the debug/build-multi single-hop lanes silently ship broken work with lying evidence.
- suggested_fix: Mirror the flagship fix: when no test files exist, write `passed:false` with message "no test evidence — solution runs but unverified (write test_solution.py)" so the gate FIX-loops toward a real test (and make the ADK builders always emit tests). Regression test: hermetic job dir with a stub patch.py / stub solution/main.py → assert gate verdict != SHIP.
- effort: S

## FINDING 2
- area: gates (EVAL.json parsing / review hop)
- severity: high
- title: Standalone `nine submit "review ..."` SHIPs a fabricated "Verdict: PASS" when there is no EVAL.json to review — grep on a missing file falls into the PASS branch and cites evidence that never existed
- evidence: nine/chains/flagship.py:299-311 (`_review_command`: `if grep -qE '"passed"...false|"exit_code"...[1-9]' EVAL.json; then ... FAIL ... else echo 'Verdict: PASS' >> review.md; echo 'Evidence: EVAL.json all checks passed, self-test exited 0' >> review.md; fi`) — when EVAL.json does not exist, grep exits 2 → else branch → PASS. Repro (hermetic, no API key — review hop is all-bash): `NINE_LEDGER=/tmp/t3-review/ledger.jsonl NINE_WORKDIR=/tmp/t3-review/work .venv/bin/nine submit "review my solution"` in a fresh dir → routes to workflow `review`, job dir contains ONLY task.txt (no code, no EVAL.json) → output `[verdict] SHIP - all evidence checks passed`; review.md = `# Review\nVerdict: PASS\nEvidence: EVAL.json all checks passed, self-test exited 0`; review-eval node then writes its own passing EVAL.json, and `_review_verdict_consistent` (flagship.py:322-343) compares review.md against that SELF-generated EVAL.json, so the gate always agrees. No code was ever read.
- impact: The review lane certifies "PASS" for a task with zero artifacts and no evidence, and the shipped review.md carries a fabricated evidence citation ("EVAL.json all checks passed, self-test exited 0" — there was no self-test and the EVAL.json cited is the review's own echo). QA theater survives in the standalone review lane (T1-F3/T2-F1 fixed the chain path only); "review this code" can never fail and never reads the code.
- suggested_fix: In `_review_command`, fail (exit 1, no PASS) when EVAL.json is missing or unreadable (`if [ ! -f EVAL.json ]; then echo 'Verdict: FAIL' ...; exit 1; fi`); additionally have `_review_verdict_consistent` compare review.md against the BUILD's EVAL.json (snapshot before review-eval overwrites), not the review's own output. Regression test: empty job dir → `nine submit "review x"` must NOT reach SHIP; review.md must not contain "PASS".
- effort: S

## FINDING 3
- area: ledger (recover/cleanup)
- severity: medium
- title: `nine recover` on a shipped/running/awaiting_evidence job crashes with a raw InvalidTransition traceback — recover() silently no-ops, then the executor dies on the illegal `shipped -> running` transition
- evidence: nine/ledger/ledger.py:194-200 (`recover` only transitions when status in ("blocked","failed"); any other status returns the job unchanged — no error), nine/cli.py:339-372 (`cmd_recover` never checks job.status after `ledger.recover`, then calls `_execute_job`), nine/runtime/workflows.py:113 (`if job.status == "submitted": job.transition("routing")` is skipped for a shipped job) + workflows.py:121 `job.transition("running")` → `LEGAL_TRANSITIONS["shipped"] = {"archived"}` (nine/ledger/ledger.py:25) → `InvalidTransition: illegal transition shipped -> running`, which `cmd_recover` does not catch (only LedgerError). Repro: `.venv/bin/python`: submit job, walk it to shipped, `ledger.recover(job_id)` returns the job with status still `shipped`; then `WorkflowExecutor(...).execute(wf, job, ...)` raises `InvalidTransition`. Same crash for recover on `running`, `awaiting_evidence`, `fixing`, `recovered`, `cancelled` jobs.
- impact: `nine recover <job_id>` on anything but blocked/failed prints a raw traceback instead of a clean refusal; the CLI help says "recover a blocked/failed job" but nothing enforces it. Also, a recovered job keeps its accumulated `attempts`, so after `recovered -> running` the in-engine fix loop condition `job.attempts <= job.max_fix_loops` (workflows.py:329) is immediately false — a job that blocked after burning its fix loops gets ZERO fix loops on recovery and re-blocks on any FIX.
- suggested_fix: In `ledger.recover` (or cmd_recover), raise `LedgerError` unless status is blocked/failed; in cmd_recover catch it and print a clean message. Reset `job.attempts = 0` (and cap) on recover so the re-execution gets a full fix-loop budget. Regression test: recover on shipped raises LedgerError; recover on failed job re-executes with attempts=1 after the run.
- effort: S

## FINDING 4
- area: ledger (durability)
- severity: medium
- title: One corrupt or partial line in the JSONL ledger bricks every `nine` command — _load has no skip/repair and crashes on any malformed record
- evidence: nine/ledger/ledger.py:138-147 (`_load` does `json.loads(line)` with no try/except and `rec["workflow_id"]` / `rec["job_id"]` KeyError on any record missing the key). Repro: ledger.jsonl containing one valid job line plus one truncated line (`{"job_id": "j2", "workflow_id": "build", "status": "run`) → `JSONLLedger(path)` raises `json.JSONDecodeError: Unterminated string...`; a line `{"status": "shipped"}` → `KeyError: 'workflow_id'`. The ledger is append-only and written with a single open/write per record (ledger.py:149-152), so a crash mid-write (SIGKILL, disk full, power loss) or a hand-edit leaves exactly this state — and every `nine submit/status/artifacts/stats/recover` call loads the whole file at construction and dies.
- impact: Total availability failure of the durable audit trail: one bad byte makes nine unusable for ALL jobs (not just the damaged one), with no recovery path except manual file surgery. The docstring promises "append-only for auditability" — that audit log should survive a partial append.
- suggested_fix: In `_load`, wrap each line's parse in try/except: skip non-dict/malformed lines (or if the LAST line is malformed — the common crash-tail case — trim it and continue, optionally recording a warning count in `stats()`), so one partial write cannot take down the ledger. Regression test: ledger with valid + truncated tail line loads, healthy jobs intact, malformed line counted.
- effort: S

## FINDING 5
- area: runtime (node execution)
- severity: medium
- title: Node.timeout_seconds is ignored for prompt/tool/subagent nodes — a hung Gemini/tool call leaves the job "running" forever
- evidence: nine/runtime/workflows.py:45 declares `timeout_seconds: int = 300` on Node with the docstring "per node"; it is enforced ONLY in the bash branch (workflows.py:127 `sp.run(..., timeout=node.timeout_seconds, ...)`); the prompt/tool/subagent branch (workflows.py:134-135 `out = node.run(inputs, job_dir)`) runs the callable unbounded. The ADK nodes (nine/runtime/adk_runtime.py:80-96 `list(self.runner.run(...))` with no timeout on the model call) and responder/summarizer `generate_content` calls (nine/runtime/summarizer.py:36 — note `_gemini_generate(..., timeout: int = 90)` accepts a timeout parameter and never passes it to `generate_content`) can hang indefinitely on a stalled network/free-tier request. Repro (static + behavior): a tool node whose `run` sleeps forever with `timeout_seconds=1` — `WorkflowExecutor.execute` never returns, job stays `running`, no WorkflowError is ever raised; only manual `nine cancel` can end it. In chains, a hung hop keeps the chain job `running` forever too.
- impact: Jobs (and whole chains) can wedge permanently in `running` with no watchdog; free-tier Gemini stalls are normal, and nothing bounds them. Every node declares a timeout that only bash honors — the contract silently lies for 4 of 5 node kinds.
- suggested_fix: Wrap non-bash node runs with a real deadline (e.g. run the callable in a thread and `join(timeout_seconds)`, or use `asyncio.wait_for` around the ADK runner's async generator; pass `timeout=` to genai `generate_content` in summarizer/responder). On timeout raise WorkflowError so the job fails loud (or retries per max_retries). Regression test: node run sleeping 1s with timeout_seconds=0.1 → execute raises WorkflowError within ~1s and job transitions to failed.
- effort: M

## FINDING 6
- area: runtime (FIX loop / Gemini quota)
- severity: medium
- title: Every in-engine FIX attempt re-runs ALL nodes — a failing gate check re-burns the full Gemini budget (ADK node × 3 internal retries × all nodes × fix loops) even when only one bash check failed
- evidence: nine/runtime/workflows.py:230-247 (the attempt loop runs `for nid in order` over every node on every attempt; `node_outputs` from the previous attempt are retained but NOT used to skip re-execution) + workflows.py:329 (`FIX ... continue`). Repro: workflow with a "gemini" tool node (counts invocations, writes art.txt) + a verify node that fails the gate on attempt 1 and passes on attempt 2 → `execute` returns SHIP in 2 attempts, and the gemini node ran TWICE (once per attempt) even though its output never changed and only the verify check failed. With real ADK nodes each run is 1-3 Gemini calls (adk_runtime.py:57-76 retries 429/503 up to 3×), so a single late-gate failure costs up to 3 calls × 3 fix attempts = ~9 model calls; chain hops repeat this per hop (chain.py `while attempt <= hop.max_fix_loops` re-submits the whole hop workflow).
- impact: Wastes Gemini quota (free tier = 20 req/day) on repeated identical model runs; a workflow whose deterministic verify fails (e.g. missing test file) re-runs the expensive model nodes pointlessly. Directly contradicts the fix-loop docstring intent ("rework the artifacts and re-run" — only failing artifacts need rework).
- suggested_fix: Skip re-running nodes whose (inputs, dependency outputs, and produced artifact hashes) are unchanged from the previous attempt — cache per-node results keyed by those hashes (bash/deterministic nodes that already exited 0 and whose outputs didn't change can be replayed from the cache; only nodes affected by fix_directive re-run). Regression test: gemini-count repro above with the fix in place → gemini node invoked exactly once while the verify node re-runs.
- effort: M

## FINDING 7
- area: runtime (FunctionTool error handling / tool sandbox)
- severity: medium
- title: ADK write_file tools accept arbitrary paths — `../` lets the model write outside its job dir (cross-job EVAL.json poisoning, catalog/ledger tampering)
- evidence: nine/chains/flagship.py:45, 125, 202; nine/workflows/debug_wf.py:49; nine/workflows/build_multi_wf.py:51 — every `write_file(path, content)` helper is `(job_dir / path).write_text(content, encoding="utf-8")` with no containment check, so `path="../x"` or `path="../../work/<other_job>/EVAL.json"` resolves outside the job dir. Repro: from `/tmp/t3-chainjob`, `write_file("../ESCAPED.txt", "pwned")` → file lands at `/tmp/ESCAPED.txt` (verified; `Path(job_dir/"../ESCAPED.txt").resolve()` is outside job_dir and the write succeeds). The model controls `path`; tasks are user-supplied, so a task like "first write ../../work/<id>/EVAL.json with passed true" (or a confused/adversarial model) can overwrite another job's EVAL.json, research/plan artifacts, the router catalog (nine/router/catalog.json — the LEARN-loop write target), or the ledger itself.
- impact: Cross-job artifact tampering: one job's model can make ANOTHER job's gate see a fabricated passing EVAL.json (ship-broken-work via poisoning), or corrupt the shared catalog/ledger. The evidence-gated model boundary (job dir) is not actually enforced at the filesystem layer.
- suggested_fix: In each write_file helper, resolve the target and refuse writes outside job_dir: `target = (job_dir / path).resolve(); if not target.is_relative_to(job_dir.resolve()): raise ValueError(...)` (and make the tool return the error so the agent sees it). Regression test: call the helper with `../escape.txt` → raises, no file written outside; `sub/file.txt` with a pre-created `sub/` works.
- effort: S

## FINDING 8
- area: ledger (redaction edge cases)
- severity: low
- title: redact() is case-sensitive — uppercase credential forms (`API_KEY=`, `PASSWORD=`, `TOKEN=`) leak verbatim into the ledger, learn-loop events, and memory graph
- evidence: nine/router/classifier.py:46-62 — all redaction patterns are lowercase-only and `re.sub` is called without `re.IGNORECASE` (pattern 1 `(password|passwd|pwd|secret|token|api[_-]?key)\s*[=:]\s*\S+`). Repro: `redact("my API_KEY=sk-*** and PASSWORD=hunter2")` → `'my API_KEY=sk*** and PASSWORD=hunter2'` — the sk- prefix is truncated by the separate `(sk|pk|ghp|gho|AIza)` pattern but `PASSWORD=hunter2` survives intact; `redact("API_KEY=abc123")` (short value) → unchanged. The ledger stores `input={"task": redact(args.task)}` (cli.py:270), and learn-loop/memory records store `redact(...)` outputs — so uppercase secrets persist in ledger.jsonl, route events, and the memory graph.
- impact: The docstring claims redaction "reduces accidental secret leakage in logs", but any task phrased with uppercase `API_KEY=`/`PASSWORD=`/`TOKEN=` (extremely common in tasks like "my token is ..." / "use API_KEY=...") leaks the credential verbatim into every durable store. Low severity (the code says "not a security boundary") but it is a one-line fix with a trivial regression test.
- suggested_fix: Add `flags=re.IGNORECASE | re.DOTALL` to the `re.sub` call (patterns are case-insensitive-safe), keep `sk/pk/ghp/gho/AIza` truncation as-is. Regression test: assert `PASSWORD=hunter2`, `API_KEY=abc123`, `TOKEN: xyz` forms are all redacted.
- effort: S
