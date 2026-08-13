# TORTURE-TESTER-7 Report — attack surface: chain + gate composition + plugins + server/API

Worker: TORTURE-TESTER-7 (round 5: chain hop re-run semantics, hop gate × manifest
composition, compose/plugin install surface, HTTP server surface)
Repo HEAD: 28d4a85. All repros hermetic (`/tmp` scratch, `.venv/bin/python`, no
GEMINI_API_KEY, no ADK/model nodes invoked — every flagship model node was reached
only through its no-key fail-loud guard or fakes). Full suite at HEAD:
288 passed, 5 skipped. LEDGER rows T1–T6 / S24 read first; nothing below re-files
a fixed finding — F7 is an *incomplete sweep* of T6-F1 (gate side fixed, manifest
side not), flagged as such.

## FINDING 1
- area: chain
- severity: medium
- title: A hop FIX re-run can SHIP on STALE disk evidence (a previous attempt's EVAL.json) that is then MISSING from the shipped manifest — the certified evidence is not in the shipped record
- evidence: the gate reads files from disk (`nine/gates/evidence.py:78-90` `load_eval_json`, `:161-168` `required_artifact_check`) while the manifest is a per-attempt snapshot (`nine/runtime/workflows.py:241` `job.artifacts = []`, `:249-254` `before` snapshot, `:305-321` registration skips anything unchanged since the attempt start). On a hop FIX re-run (`nine/chains/chain.py:181,224-227`) the SAME job_dir keeps attempt-1 files; if the fix attempt does not rewrite EVAL.json, the gate reads attempt-1's EVAL.json and returns SHIP, but attempt-1's files are never registered to the attempt-2 hop job (they are in `before`), so the certified EVAL.json is absent from the shipped manifest. Repro (hermetic, full trace in `/tmp/repro1.py`):
  ```
  attempt 1: writes EVAL.json {"checks":[...{"passed":true}]} but NOT REQ.md
             -> gate FIX (missing artifact)
  attempt 2: writes ONLY REQ.md
  final: SHIPPED   hop2 artifacts: ['REQ.md']
  hop2 verdict: SHIP evidence_refs: [REQ.md]  eval_results: {"eval": {"passed": true, "message": "1 checks passed"}}
  EVAL.json in shipped hop manifest? False   chain job artifacts: ['REQ.md']
  ```
  The SHIP verdict cites "1 checks passed" on an EVAL.json that exists in no shipped manifest (hop job or chain job).
- impact: any hop whose EVAL.json is written by the node itself (every compose-generated plugin; any model-written EVAL.json) can certify attempt-N+1's artifacts with attempt-N's EVAL.json, and the shipped chain job's evidence set is self-contradictory — an auditor of the shipped artifacts cannot find the EVAL.json the verdict cites. This is the exact "missing-but-required artifacts in hop 2" + "handoff artifact staleness" hazard: gate evidence and shipped manifest diverge.
- suggested_fix: the gate should evaluate against the attempt's registered artifact set, not raw disk — pass the manifest to checks (or snapshot the dir at attempt start and treat files unchanged since `before` as non-evidence for SHIP decisions: a SHIP must have at least one artifact registered this attempt AND every file the gate certifies must be in the manifest). Regression test: the repro above — assert `EVAL.json` IS in the shipped manifest whenever eval_json_check passed, or that the gate BLOCKs when the certifying file was not produced this attempt.
- effort: M

## FINDING 2
- area: chain
- severity: medium
- title: Flagship ADK hops IGNORE fix_directive — hop FIX re-runs are blind retries that re-burn model budget and BLOCK instead of converging
- evidence: the chain engine builds the rework directive (`nine/chains/chain.py:224-227` `"hop {hop.id} failed gate (attempt {attempt}): {reason}; rework and re-run."`) and the executor delivers it into node inputs (`nine/runtime/workflows.py:268` `"fix_directive": inputs.get("fix_directive", "")`) and into the hop job's durable input (`chain.py:177`). But `nine/chains/flagship.py` has ZERO `fix_directive` reads (`grep -n fix_directive nine/chains/flagship.py` = empty): `_research_adk_node._run` (flagship.py:36-64), `_plan_adk_node._run` (:98-124) and `_build_adk_node._run` (:163-196) build their LlmAgent instruction from `task` (+ HANDOFF.md/PLAN.md) only, so attempt 2's prompt is byte-identical to attempt 1's. Contrast: `nine/workflows/debug_wf.py:39,122` DO consume `fix_directive`. Verified with a real chain FIX loop (repro9): attempt-2 node inputs and hop-job input both carry the directive; the flagship `_run` bodies never read it. Each flagship hop also declares `max_retries=2` (node level) inside a `max_fix_loops=2` hop loop — up to 9 Gemini calls per hop before BLOCK, all blind.
- impact: when a flagship research/plan/build hop fails its gate (short research.md, bad code, missing artifact), the "rework and re-run" contract is a no-op: the model is told nothing about what failed, so the fix attempt is a coin flip that typically reproduces the same artifact, burns 2-3× Gemini budget (free-tier quota is the scarce resource), and the chain BLOCKs where a directive-aware re-run could have converged.
- suggested_fix: append the fix_directive to each flagship ADK instruction (e.g. `f"\nPrevious attempt failed the gate: {fix_dir}\nRework the artifacts accordingly."`), mirroring debug_wf. Regression test: fake-node chain where attempt 1 writes a failing EVAL.json and attempt 2 asserts the node input `fix_directive` contains the failing check names (assert the flagship `_run` receives it — and a lint/unit test that each flagship hop instruction template includes `fix_directive`).
- effort: S

## FINDING 3
- area: plugins
- severity: high
- title: compose installs plugins with NO collision check — a generated plugin whose id matches a BUILT-IN workflow id silently REPLACES the production lane (WORKFLOWS.update), with a mismatched gate and no uninstall
- evidence: `nine/workflows/compose_wf.py:228-232` — the implement node's `write_file` writes `{wfid}_wf.py` into the real repo plugins dir with no existence check (overwrites an existing plugin file unconditionally); the register node (`compose_wf.py:333-339`) is idempotent ("already registered"), so a stale registry entry silently re-points at the overwritten module; `_compose_check` (`compose_wf.py:387-400`) only requires the file to exist and be >=100 bytes. At load time `nine/registry.py:195` `WORKFLOWS.update(_load_plugin_workflows())` merges plugin ids LAST, so `PLUGIN_WORKFLOWS["build"]` (or "review"/"compose"/"test") REPLACES the built-in workflow, while `workflow_gate("build")` (registry.py:163 + `_HOPS`) still returns the BUILT-IN build gate. Repro (hermetic, `/tmp/repro3.py`): a temp plugin registry declaring `build_hop` → `WORKFLOWS["build"]().description == "PLUGIN clobber"` while `workflow_gate("build")` still demands solution.py/EVAL.json; the router still routes "build a calculator" to id `build`. A compose job whose model writes `WF_ID.txt: build` (or `review`, `compose`, ...) therefore SHIPs a plugin that hijacks the built-in lane for every future submit — the lane either runs the plugin under the wrong gate (FIX-loop to BLOCK) or, if the plugin happens to produce the gate's artifacts, SHIPs plugin behavior as "build".
- impact: silent production lane hijack / breakage from a single compose run (or a confused/malicious model during compose); a blocked compose run also leaves a broken or unwanted `{wfid}_wf.py` in the repo plugins dir permanently (no cleanup, no uninstall, no overwrite warning). There is no way to remove a plugin today.
- suggested_fix: refuse to install when `wfid` collides with any built-in workflow id (registry.WORKFLOWS/CHAINS keys) or an existing plugin (`_PLUGINS_DIR/{wfid}_wf.py` exists → require explicit `wfid` change or a versioned suffix, and fail the gate with a clear message); add `nine plugin uninstall <id>` that removes the file + registry lines; make `WORKFLOWS.update` refuse to shadow built-in ids (or log a loud warning + route the built-in). Regression test: compose job with `WF_ID.txt: build` → gate BLOCKs with "id collision", built-in `WORKFLOWS["build"]` untouched.
- effort: M

## FINDING 4
- area: chain
- severity: medium
- title: CLI chain runs attach a FABRICATED route decision — chain jobs record route_decision.workflow_id "respond"/"build" (conf 0.0), and every chain-hop LEARN event carries that bogus confidence
- evidence: `nine/chains/chain.py:151-159` — when `decision is None` (the CLI `nine chain` path, `cli.py:125-146` passes nothing), ChainExecutor derives a KEYWORD decision from the TASK: `decision = _r.classify(str(inputs.get("task", "")))` and `job.attach_route_decision(decision)`. Repro (hermetic, `/tmp/repro4.py`): `nine chain inbox-triage-task-report "customer wants a refund"` → chain job `workflow_id="inbox-triage-task-report"` but `route_decision.workflow_id="respond"`, `confidence=0.0`, `model="deterministic-keyword"`. Every hop RouteEvent then records `confidence=float(decision.confidence)` = 0.0 (`chain.py:211-213`) with `workflow_id="inbox-triage-task-report::triage"` — the LEARN loop (`learner.py:225-234`) sees 0.0-confidence events for real SHIPped chain hops and proposes bogus "add keyword" candidates (`_derive_keyword` on `chain::hop` ids that are not in WORKFLOWS). `nine chain flagship "build a calculator"` similarly stamps route_decision.workflow_id="build".
- impact: chain jobs in the ledger are self-contradictory (the decision says `respond`, the job ran a chain); chain-run route events pollute LEARN statistics with confidence 0.0 and wrong workflow ids; any consumer of route_decision (recover restores and re-uses it, `cli.py:209-218`) propagates the lie into the re-run.
- suggested_fix: when decision is None, synthesize an honest RouteDecision whose workflow_id is the CHAIN id (confidence 1.0, reason "explicit chain invocation") instead of classifying the task; or skip attach_route_decision for explicit chain runs. Regression test: `ex.execute(demo_lane(), job, {"task": ...})` with decision=None → assert `job.route_decision["workflow_id"] == "inbox-triage-task-report"` and LEARN events carry that id.
- effort: S

## FINDING 5
- area: robustness
- severity: low
- title: `nine recover` on a chain job raw-tracebacks when a hop fails loud — the chain branch of `_execute_job` is missing the `except ChainError` that `nine chain` has
- evidence: `nine/cli.py:170` (`cmd_chain`) catches ChainError and prints a clean one-line `[error] ... failed loud`; `_execute_job`'s chain branch (`cli.py:228-232`) — the path `cmd_recover` takes (`cli.py:394`) — has no such catch. Repro (hermetic): seed a blocked `research-plan-build-review-teach` job + task.txt, run with GEMINI_API_KEY unset → `nine recover` prints a 20-line ChainError traceback (`hop research crashed: ... GEMINI_API_KEY ...`) with exit 1 and NO clean one-line error; the same failure via `nine chain flagship` exits 1 with exactly one clean line and no traceback. (Ledger state is handled: the job ends `failed` — this is a UX/contract gap, not data loss.)
- impact: contradicts the T2-F7/T4-F2 doctrine ("every CLI error path exits non-zero with a message" — clean one-line, no raw traceback) on a mainline recovery path; operators recovering a blocked chain during a quota/key outage get a wall of frames instead of the actionable one-liner.
- suggested_fix: wrap the chain branch of `_execute_job` in `except ChainError as exc: print(f"[error] ... failed loud: {exc}", file=sys.stderr); return 1` (mirror cmd_chain). Regression test: recover a blocked chain job with no API key → assert stderr has one `[error]` line and no `Traceback`, exit 1.
- effort: S

## FINDING 6
- area: server
- severity: medium
- title: POST /v1/submit 1 MiB cap is bypassable with chunked/streamed bodies — `_guard` only checks the content-length HEADER
- evidence: `deploy/server.py:189-191` — `cl = request.headers.get("content-length"); if cl and cl.isdigit() and int(cl) > MAX_BODY_BYTES: 413`. A client that streams the body with chunked transfer-encoding sends no content-length, so the guard is skipped and Starlette/FastAPI buffers the entire body before pydantic validation. Repro (hermetic, `/tmp/repro6b.py`): a ~1.3 MB chunked JSON body (no content-length header) is fully accepted and processed (routed to `respond`, which then 502s only because no model is configured) — the 413 path is never taken; a 1 GiB stream would be fully buffered.
- impact: on the deployed Cloud Run API the documented 1 MiB cap is a DoS hole: any caller can stream an unbounded body, forcing full buffering + JSON parse in memory (Cloud Run container OOM) and defeating the pydantic task-length bound as a cost control (the body must be fully read before the 2000-char task cap is enforced).
- suggested_fix: enforce the cap on the body itself in the middleware (`request.stream()` with a byte counter, or check `content-length` AND wrap the body read with a streaming limit; FastAPI/Starlette body size limits or a `BaseHTTPMiddleware`/ASGI-level read cap). Regression test: chunked body > 1 MiB → 413 (or connection-level 413) without buffering.
- effort: S

## FINDING 7
- area: robustness
- severity: low
- title: T6-F1's "symlinks never evidence" fix is incomplete — the manifest registration loop still hashes and certifies symlink TARGETS as job evidence (outside-file sha256 lands in shipped records)
- evidence: T6-F1 fixed the GATE side (`nine/gates/evidence.py:83,102,167` all check `is_symlink`) but the artifact-registration loop T6-F1 cited is unchanged: `nine/runtime/workflows.py:305-321` — `if not p.is_file(): continue` (is_file() FOLLOWS symlinks, verified on the repo venv), then `p.stat()` + `p.read_bytes()` + sha256, with no `p.is_symlink()` guard. Repro (hermetic, `/tmp/repro7g.py`): a node writes solution.py + test_solution.py + EVAL.json (passing) and `data.txt -> /etc/passwd-like outside file`; the job SHIPs and the shipped manifest contains `data.txt` with the OUTSIDE file's sha256/size, `produced_by` = the node. The gate still refuses a symlinked EVAL.json (so the SHIP path is not fully broken open), but the ledger certifies outside-content fingerprints as job evidence, and chain `_save_memory` (`nine/chains/chain.py:246-247,272-289`) persists that sha256 into the semantic memory store.
- impact: shipped evidence manifests (and semantic memory summaries) contain hashes/sizes of files that were never produced in the workspace — the exact "certifies OUTSIDE file content as job evidence" lie T6-F1 was filed for, still reachable through the registration site; an operator auditing `nine artifacts`/memory sees outside-file fingerprints attributed to the job.
- suggested_fix: add `if p.is_symlink(): continue` (or `lstat` + `is_file(follow_symlinks=False)`) in the registration loop, matching the gate-side fix. Regression test: node creates `data.txt -> outside file` + a real passing EVAL.json → SHIP but assert no artifact entry has the outside sha256.
- effort: S

## FINDING 8
- area: docs
- severity: low
- title: `chain_inputs["hop_artifacts"]` handoff is dead code — the documented artifact-passing input contract is never delivered to any node
- evidence: the only assignment is `nine/chains/chain.py:248-253` (`chain_inputs["hop_artifacts"] = {a: str(job_dir/a) for a in hop.required_artifacts if exists}`); `grep -rn hop_artifacts nine deploy tests` finds the set site only — `WorkflowExecutor` builds node inputs from `task/node/job_id/attempt/fix_directive` + dependency outputs (`nine/runtime/workflows.py:264-270`) and never forwards `hop_artifacts`. The chain module docstring ("Artifacts produced by hop N ... are handed to hop N+1 via the artifact-passing contract", chain.py:9-12) and `docs/architecture.svg:73` ("artifact-passing contract") imply an input contract that no hop workflow can receive. Flagship hops survive because they hardcode filenames and share the job dir; any compose-generated chain hop written against the documented input key silently gets nothing.
- impact: doc/behavior inconsistency for plugin authors: a hop that reads `inputs["hop_artifacts"]` (per the docs) always sees `KeyError`/missing data; the engine carries dead state across every chain hop for nothing.
- suggested_fix: either actually pass `hop_artifacts` into node_inputs in `WorkflowExecutor.execute` (plus test that a fake hop sees the previous hop's artifact paths), or delete the dead assignment and reword the docstring to say the handoff is the shared job dir only. Regression test: two-hop chain where hop 2's node asserts `inputs.get("hop_artifacts")` matches hop 1's required artifacts (if the contract is kept).
- effort: S

---
Count by severity: 1 high (F3), 4 medium (F1, F2, F4, F6), 3 low (F5, F7, F8).
