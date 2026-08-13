# TORTURE-TESTER-2 — nine adversarial report (workflows + router + CLI + docs)

Surface: workflows + router + CLI + docs. All repros run read-only (temp dirs / `.venv/bin/python`), no repo files modified, no Gemini key used. 8 findings, sorted by severity.

---

## FINDING 1
- area: workflow
- severity: critical
- title: Flagship chain's research/plan/review hops are canned bash stubs — review rubber-stamps PASS on every build, and "research" artifacts are identical static text for any task
- evidence: `nine/chains/flagship.py:27` (`research_hop` bash node hardcodes `echo 'Key insight: evidence-gated execution keeps agents honest.' >> research.md`), `nine/chains/flagship.py:61` (`plan_hop` hardcodes `Steps: 1) scaffold 2) implement 3) verify with EVAL.json`), `nine/chains/flagship.py:205` (`review_hop` hardcodes `echo 'Verdict: PASS' >> review.md` then `grep -q 'PASS'`). Repro: run the three commands in a temp dir for two unrelated tasks ("research the history of the typewriter" vs "find a cure for cancer") — research.md differs only by the echoed task line and always contains the same single canned "Key insight" sentence; PLAN.md is byte-identical; review.md is always `Verdict: PASS`. The review hop's gate (`flagship.py:210-216`) only checks review.md exists + exit codes, so the rubber stamp always passes.
- impact: The flagship chain (`nine chain flagship "..."`) fabricates "findings"/"plans"/"review PASS" for every task — the exact canned-output fabrication the README/doctrines forbid ("NEVER a canned answer", `nine/runtime/responder.py:12`). A build hop that ships a stub `print("TODO")` still receives `Verdict: PASS` from review and the chain reports SHIPPED. Docs lie: README.md:88-89 claims research.md is "Gemini distill... never fabricated", registry.py:218 describes research as "Produce a findings document", and README quickstart claims `nine submit "research the history of the typewriter"` produces real findings + EVAL.json (the research hop produces no EVAL.json at all).
- suggested_fix: Replace the three stub bash nodes with real model nodes (like `_build_adk_node`): research hop = ADK agent writing task-specific findings; plan hop = ADK agent writing task-specific PLAN.md; review hop = ADK reviewer that reads solution.py + EVAL.json and writes a verdict from actual evidence, with gate checking the verdict text is evidence-derived (e.g. requires cited file:line findings) — or at minimum make review FAIL when EVAL.json check failed. Regression test: run the flagship chain against a deliberately broken build fixture and assert the chain does NOT reach `final=SHIPPED`.
- effort: M

---

## FINDING 2
- area: workflow
- severity: high
- title: Build self-test is exit-0-only when no tests exist — a stub `solution.py` that prints and exits 0 SHIPs with EVAL.json "solution-runs passed"
- evidence: `nine/chains/flagship.py:161-166` (`_build_self_test_command` else-branch: `python3 -B solution.py > build.log 2>&1; rc=$?` → on rc 0 writes `{"checks":[{"name":"solution-runs","passed":true,...}]}`). Repro: temp dir with `solution.py` = `print("TODO: implement me")` (no test_solution.py, which is the norm — the ADK builder instruction at `flagship.py:100` says "write ONE runnable Python module solution.py", so tests rarely exist) → run `_build_self_test_command()` → EVAL.json `solution-runs passed:true`; gate (`eval_json_check` + `exit_codes_check` + artifacts) → `VERDICT: SHIP`. A solution that raises SystemExit(1) does FIX, but any exit-0 stub ships.
- impact: The flagship build hop SHIPs completely unimplemented code as "verified" — the EVAL.json claim "solution builds and verifies" is a lie (nothing was tested), and the chain then rubber-stamps review PASS (FINDING 1). Same pattern in `nine/workflows/debug_wf.py` (verify falls back to `python3 -B patch.py` exit 0) and `nine/workflows/build_multi_wf.py:109-116` (verify falls back to entrypoint run / py_compile). Silently ships broken work.
- suggested_fix: When no test file exists, require the solution to be exercised against task-derived assertions: e.g. run `py_compile` AND a smoke invocation that imports the module and calls its public API (fail if no public callable), and write EVAL.json with a weaker claim ("syntax+entrypoint only") so the gate/operator can tell; better: make the build ADK agent also write `test_solution.py` (as build-multi already instructs) and treat missing tests as FIX. Regression test: stub-solution fixture must yield verdict != SHIP.
- effort: S

---

## FINDING 3
- area: workflow
- severity: high
- title: Transform workflow trusts the model-written TARGET.txt — "convert this csv to json" SHIPs with a garbage OUTPUT.txt and no JSON at all
- evidence: `nine/workflows/transform_wf.py:121` (validate reads extension from model-written `TARGET.txt`: `ext = Path("TARGET.txt").read_text()...`), `nine/workflows/transform_wf.py:115-135` (for ext not json/csv/yaml: `ok = outp.stat().st_size > 10` → "non-empty text output" passed), `nine/workflows/transform_wf.py:146-158` (`_output_check` also trusts TARGET.txt). Repro: job dir with `FORMAT.md`, `TARGET.txt` = `txt`, `OUTPUT.txt` = `blah blah...` (29 bytes, not JSON) → `_validate_command()` writes EVAL.json `transform-validate passed:true`, `_output_check` passes, gate → `VERDICT: SHIP`; `OUTPUT.json` does not exist.
- impact: The model can satisfy the gate by relabeling the target format; the requested conversion never happens yet EVAL.json says the transform validated. The "requested format" is never enforced anywhere — the gate validates whatever extension the model declares. Silently ships broken output + lying EVAL.json.
- suggested_fix: Derive the expected output extension from the task/target spec (e.g. a TARGET.txt written by the deterministic detect node from the task, not the model) and have validate + `_output_check` parse/extract the extension from `TARGET.txt` *and* require `OUTPUT.<requested-ext>` when the task names one; fail on mismatch. Regression test: fixture where model writes TARGET.txt=txt for a "to json" task → gate must BLOCK/FIX.
- effort: S

---

## FINDING 4
- area: workflow
- severity: medium
- title: FIX-loop reruns leave stale artifacts in the shipped manifest with wrong `produced_by` attribution — a failed attempt's EVAL.json is listed as a shipped artifact produced by the wrong node
- evidence: `nine/runtime/workflows.py:211` (`job.artifacts = []  # manifest = this attempt's artifacts only` — comment is false), `nine/runtime/workflows.py:212` (`seen` reset per attempt), `nine/runtime/workflows.py:249` (after every node, the whole job dir is scanned and every un-deduped file re-registered with `produced_by` = the scanning node). Repro: workflow A→B→C where A writes a.txt, B writes b.txt only if absent, C writes EVAL.json passed:false on attempt 1 / true on attempt 2 → gate FIX then SHIP. Final shipped manifest: `[('EVAL.json','A' size 40 passed:false), ('a.txt','A'), ('b.txt','A'), ('EVAL.json','C' size 39 passed:true)]` — b.txt (written by B) is attributed to A, and the stale FAILING EVAL.json from attempt 1 ships as an artifact. Same lie in chains: hop N+1 re-registers hop N's files as its own (`nine/chains/chain.py:215-217` rolls every hop artifact up to the chain job).
- impact: `nine artifacts <job_id>` / ledger / memory lineage (sha256 + produced_by + hop_id) lie about what shipped and who produced it; memory graph stores summaries keyed to wrong hops. Undermines the audit trail the ledger exists for.
- suggested_fix: Track artifacts by (name,sha256) across attempts and (a) keep a single latest entry per name in the final manifest (drop stale entries), (b) attribute produced_by from the node that actually wrote/rewrote the content — e.g. snapshot dir state before each node and only register files whose hash changed during that node. Regression test: fix-loop fixture asserting exactly one EVAL.json with produced_by=C in the shipped manifest.
- effort: S

---

## FINDING 5
- area: router
- severity: high
- title: Keyword router uses substring matching, so common English words misroute to specialist workflows — "what is the latest news" → test workflow; "water the plant" → plan workflow
- evidence: `nine/router/classifier.py:87-89` (`if kw in task_l` / `score = len(kw)/len(task)`). Repro (deterministic, no key): `Router().classify()` on real registry keywords routes: "what is the latest news on AI" → `test` (conf 0.138), "the greatest advances in robotics" → `test`, "the protest was peaceful" → `test` ("test" ⊂ latest/greatest/protest), "water the plant in the office" → `plan`, "book a plane ticket to paris" → `plan` ("plan" ⊂ plant/plane), "please plan our trip to the beach" → `plan` (instead of the inbox-triage "trip" lane, which loses on the tie since `plan` is registered earlier).
- impact: Wrong-lane jobs run real model workflows against irrelevant tasks: "latest news" runs the test workflow (ADK agent writes pytest for a news query, then pytest on nonexistent solution.py → wasted Gemini quota per fix-loop attempt, nonsense artifacts); trip/refund inbox items can be diverted to `plan`. Every misroute burns quota and pollutes the LEARN event store with confident wrong labels.
- suggested_fix: Word-boundary matching (`re.search(rf"\b{re.escape(kw)}\b", task_l)`), plus require `len(kw) >= 4` and/or drop single-meaning-embedded words ("test","plan","error") from the substrate in favor of the model router. Regression test: assert "latest"/"greatest"/"protest"/"plant"/"plane" tasks do not route to test/plan.
- effort: S

---

## FINDING 6
- area: CLI
- severity: medium
- title: Raw task text (including credentials) is stored unredacted in the job ledger and workdir while docs claim "redaction in logs"
- evidence: `nine/cli.py:189` (`ledger.submit(workflow_id=..., input={"task": args.task})` — raw task), `nine/ledger/ledger.py:70` (`self.input = input or {}`; `to_dict()` serializes input), `README.md:208` ("Secret hygiene by design: redaction in logs..."). Repro (no key needed): `nine submit "my api password is hunter2 and my token is sk-ABCDEF1234567890, write a script"` → the RouteDecision printed to stdout is redacted (`token is sk***`) but the ledger line contains `"input": {"task": "my api password is hunter2 and my token is sk-ABCDEF1234567890, write a script"}` in full; `nine status <job_id>` prints it; the same raw text is passed to every workflow node and (where applicable) written to task.txt artifacts. Also `redact()` (`nine/router/classifier.py:53-55`) only matches `password\s*[=:]\s*\S+` — "password is hunter2" (space-separated) survives even the redacted field, as does `Bearer`-style text without the pattern.
- impact: Operator secrets end up in plaintext in jobs/ledger.jsonl, in job artifacts (task.txt), in `nine status` output, and — for chains — in hop job records; contradicts the README's redaction claim; Firestore-backed deployment (`deploy/server.py` uses the same ledger contract) would persist raw credentials server-side.
- suggested_fix: Redact at the ledger boundary: store `input` with the task passed through `redact()` (or store only `task_redacted`) while keeping the full task in the job dir file only; extend redact regexes to space-separated `password|secret|token|api[_-]?key` forms. Regression test: submit a task containing `password is hunter2` and assert the ledger line and status output contain neither `hunter2` nor the raw token.
- effort: S

---

## FINDING 7
- area: CLI
- severity: low
- title: `nine cancel` / `nine recover` on an unknown job id crash with a raw Python traceback instead of a clean error (status/artifacts handle it correctly)
- evidence: `nine/cli.py:299` (`cmd_cancel` calls `ledger.cancel(args.job_id)` with no try/except), `nine/cli.py:305` (`cmd_recover` same), while `nine/cli.py:287-294` (`cmd_status`) and `cmd_artifacts` wrap `LedgerError` in a clean message. Repro: `.venv/bin/nine cancel nonexistent-id` (no key needed) → `Traceback (most recent call last): ... LedgerError: job not found: nonexistent-id`, exit 1; same for `recover`. `status`/`artifacts` print `error: job not found: ...`.
- impact: Inconsistent CLI behavior / operator-facing crash noise; the CLI docstring promises "Exit codes: 0 ok, 1 error" — a traceback is neither a clean error nor consistent UX; scripts parsing stderr get garbage.
- suggested_fix: Wrap `cmd_cancel`/`cmd_recover` in the same `try/except LedgerError -> print("error: ...")` pattern as `cmd_status`. Regression test: assert `cancel`/`recover` with unknown id return 1 with a one-line error and no "Traceback".
- effort: S

---

## FINDING 8
- area: workflow
- severity: medium
- title: summarize-standalone SHIPs "a summary of nothing" — empty workspace still passes the gate because no check requires SOURCE.md to contain actual source
- evidence: `nine/workflows/summarize_standalone_wf.py:46` (read-source bash: when no `solution.py`/`solution/` exists it writes `- (no source files found)` into SOURCE.md and `exit 0`), `nine/workflows/summarize_standalone_wf.py:86` (gate: only `exit-codes`, artifacts exist, and `file_nonempty_check("SUMMARY.md", min_chars=20)`). Repro: temp dir with no source; run read-source command → SOURCE.md = "# Source (for summarization) | ## Inventory | - (no source files found)"; write any ≥20-char SUMMARY.md (e.g. "No source files were found in the workspace; nothing to summarize.") → gate evaluate → `VERDICT: SHIP`.
- impact: A user who submits "summarize the code" in a workspace without code gets a SHIPPED job whose SUMMARY.md is the model's guess/boilerplate about an empty directory — the artifact claims a summary was produced, and the model spends quota to "summarize" nothing. Same pattern in transform/analyze (detect nodes write "No input file found" and exit 0, then the model proceeds on empty input).
- suggested_fix: Add a gate check that SOURCE.md (or the detected input) contains real content lines beyond the inventory header — e.g. `required_artifact_check` on a marker plus a `file_nonempty_check("SOURCE.md", min_chars=...)` after stripping header lines, or have the read-source node exit non-zero / write EVAL.json passed:false when no source is found. Regression test: empty-workspace fixture must not SHIP.
- effort: S
