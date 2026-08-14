# TORTURE-TESTER-10 Report — slice-28 provider-switch fallout + CLI/router/gate/registry edges

Worker: TORTURE-TESTER-10 (round 5: CLI control-plane, evidence-gate manifest truth,
slice-28 NINE_LLM_BACKEND fallout, router keyword edges, plugin registry, bench harness, docs)
Repo HEAD: 4299677 (slice 29: fixture bugfix-small-009; slice 28 = LLM provider switch).
All repros hermetic (no Gemini, no network): `.venv/bin/python` scripts in /tmp, stub/monkeypatch
only, no API keys printed. READ-ONLY: no repo files touched, no git operations.

Re-attacked surfaces that HOLD (not re-filed): ADKAgentNode empty-stream fail-loud, eval-gate
strict-boolean EVAL contract, keyword word-boundary matching, recover's task.txt/symlink guards,
ledger submit redaction boundary, cancel-vs-executor race polling (T8-F3), symlink
non-evidence in manifest + gate, compose plugin-collision gate (T7-F3). Findings below are NEW:
the T7-F1 stale-EVAL guard is incomplete (other artifacts can still SHIP stale), recover --force
is broken end-to-end, the slice-28 tunnel message builder duplicates tool results, the plugin
registry merge has no collision guard, `create the` hijacks docs tasks, bench is backend-blind
(contradicting llm_provider's own docstring), and two slice-28 docs/error-message lies.

---

## FINDING 1
- area: CLI
- severity: high
- title: `nine recover --force` mutates the durable ledger to failed, then errors on the stale in-memory cache — the documented "degrades it to failed and re-executes" never re-executes (exit 1, needs a second invocation)
- evidence: `nine/cli.py` cmd_recover --force path does `live = ledger.refresh(id)` (durable read), `live.transition("failed"); ledger.update(live)` — but `update()` only appends a line (`ledger.py: _append`), it NEVER updates the `_jobs` cache; `refresh()` is explicitly documented "Deliberately does NOT rebuild self._jobs" (`ledger.py`). Then `ledger.recover(id)` calls `self.get(id)` which reads the CACHE → still `running` → `LedgerError("job X is running, only blocked/failed can be recovered")`. Repro (hermetic, `/tmp/repro_recover_force.py`, crash-left `running` job + task.txt):
  ```
  cmd_recover rc: 1
  durable status after --force attempt: failed     <- state WAS mutated
  error: job ... is running, only blocked/failed can be recovered   <- cache lies
  ```
  Second invocation (no --force) then re-executes and fails loud only because respond needs a key (model-or-fail) — the point is the FIRST --force call returns rc 1 while claiming to re-execute. Zero test coverage for --force in tests/.
- impact: the operator-facing crash-recovery path (T8-F6's own fix) is broken: a crash-left `running` job requires TWO recover calls, the first leaves the durable ledger in a `failed` state the operator never confirmed, and the error message contradicts the durable truth. Any scripted/automated recovery (bench, runbook) fails on the first attempt.
- suggested_fix: after force-degrading, either mutate the cache (`ledger._jobs[id] = live` or re-run `ledger.recover(live)`) or make `recover()` re-read durable state when the cache says `running`; add a `--force` regression test asserting one call transitions failed → recovered → re-executes.
- effort: S

## FINDING 2
- area: runtime (evidence gate)
- severity: high
- title: The T7-F1 stale-EVAL guard only covers EVAL.json — every other disk-read gate check (required_artifact_check, valid-json, sections) can certify a STALE attempt-1 file that is NOT in the shipped manifest (SHIP without evidence)
- evidence: `nine/runtime/workflows.py` — the stale guard fires only `if verdict["verdict"] == "SHIP" and "eval-json" in self.gate.checks`. `required_artifact_check` (`evidence.py`) checks `(workdir/e).exists()` — pure disk, no per-attempt provenance; extract's gate has NO eval-json check at all so the guard does not even apply to it. Repro (hermetic, `/tmp/repro_stale_artifact.py`): node A writes `artifact.txt` on attempt 1 and "succeeds" without rewriting on the FIX rerun (exactly what happens when an ADK agent returns text but skips its write_file tool call — see debug_wf patch node / research_quick researcher); node B rewrites EVAL.json passed=true on attempt 2:
  ```
  verdict: SHIP        summary: all evidence checks passed
  attempts: 2
  manifest (this attempt): ['EVAL.json']
  artifact.txt in manifest: False
  gate certifies artifact.txt: True      <- the lie
  ```
  Real reachability: debug FIX rerun where the patch agent skips write_file but the flaky pytest rerun passes → SHIP certifies the attempt-1 patch.py, absent from the manifest; extract/research-quick have the same shape.
- impact: the core VERIFY promise ("nothing ships without evidence; manifest = this attempt's artifacts") is violated: a SHIP can certify required artifacts produced in a PREVIOUS attempt under a different fix directive. Same lie class T7-F1 fixed for EVAL.json — unfixed for every other artifact.
- suggested_fix: generalize the T7-F1 guard: for a SHIP, every file the gate's artifact/existence checks depend on must be in this attempt's registered manifest (e.g. compare `required_artifacts` ∩ disk-exists against registered names; BLOCK otherwise). Add a FIX-loop regression test with a node that skips its write on rerun.
- effort: S

## FINDING 3
- area: runtime (slice-28 provider switch)
- severity: high
- title: Tunnel backend duplicates every tool result — `_messages_from` appends function_response parts TWICE (once in the `role == "tool"` branch, once by the trailing `if tool_msgs: out.extend(tool_msgs)`), breaking ADK tool rounds in testing mode
- evidence: `nine/runtime/llm_provider.py` `_messages_from`: for a tool-response Content, `elif role == "tool": out.extend(tool_msgs)` runs and then the unconditional `if tool_msgs: out.extend(tool_msgs)` runs again. Repro (hermetic, /tmp/repro_toolmsg_double.py — exact function source, stub Content/Part with one tool round):
  ```
  tool messages: 2 (expected 1)
  AssertionError: BUG: tool message duplicated 2x
  ```
  The debug/build/research-quick/research-deep/transform ADK agents all use FunctionTool(write_file) — every tool call in testing mode sends the result twice.
- impact: with NINE_LLM_BACKEND=openai (the documented quota-exhaustion mode), every ADK tool loop is malformed: the tunnel model sees duplicated tool results and the assistant↔tool message alternation is broken; strict OpenAI-compatible servers may reject the sequence outright; multi-tool-call builds/debug runs degrade or fail. The whole point of slice 28 (run the same model nodes on the tunnel) is compromised for tool-bearing lanes.
- suggested_fix: delete the trailing `if tool_msgs: out.extend(tool_msgs)` (the `role == "tool"` branch already extends) or guard it with `elif`. Add a unit test for a full user→assistant(tool_call)→tool round asserting exactly one tool message.
- effort: S

## FINDING 4
- area: registry
- severity: medium
- title: `WORKFLOWS.update(_load_plugin_workflows())` silently replaces core workflow ids — a plugin named "research" hijacks every "research" submit with zero warning
- evidence: `nine/registry.py: `WORKFLOWS.update(_load_plugin_workflows())` has no collision detection (the compose gate T7-F3 blocks NEW collisions at compose time, but the dispatch registry — the single source of truth — re-validates nothing). Repro (hermetic, /tmp/repro_plugin_shadow.py — temp plugin_registry.py with `PLUGIN_WORKFLOWS = {"research": ...}`, NINE_PLUGIN_REGISTRY env):
  ```
  workflow id: research
  description: PLUGIN SHADOW workflow
  CORE research replaced by plugin: True
  "research" routable via KEYWORDS: True
  ```
  The router still routes `research ...` tasks to workflow id `research`, which now executes the plugin's nodes (plugin ids are NOT in KEYWORDS — unroutable on their own, so a plugin only ever runs if it shadows a core id or is invoked explicitly).
- impact: a hand-edited/stale/copied plugin_registry.py (or one written before the T7-F3 guard) silently changes what production `nine submit "research ..."` jobs execute — no warning, no log, no gate difference. Silent behavior change in a system whose doctrine is "never changes behavior silently".
- suggested_fix: in `_load_plugin_workflows()` (or the update call site), intersect plugin ids with WORKFLOWS/CHAINS keys and print a loud warning + skip (or require explicit opt-in) for colliding ids; the compose gate should ALSO refuse to register an id that already exists in the registry file.
- effort: S

## FINDING 5
- area: router
- severity: medium
- title: The `create the` build keyword hijacks documentation/writing tasks — "create the readme", "create the report", "create the summary" route to build (solution.py + pytest self-test) instead of document/summarize
- evidence: `nine/registry.py` `_BASE_KEYWORDS`: build includes the bare phrase `create the`; document/summarize only have narrower keywords. KeywordRouter scores `len(kw)/len(task)` (classifier.py), so:
  ```
  'create the readme'    -> build (kw='create the', 0.529)  vs document 'readme' (0.353)
  'create the report'    -> build (kw='create the', 0.588)
  'create the summary'   -> build (kw='create the', 0.471)  vs summarize 'summary' (0.368)
  ```
  The build hop's ADK agent is instructed to write `solution.py` and the self-test runs pytest/python on it; a markdown docs task therefore FIX-loops into a BLOCKed job (or worse, an agent that "solves" it by writing non-python code as solution.py). The word-boundary `create the` makes it a prefix catch-all for every "create the <noun>" task.
- impact: common real-world phrasings for documentation and content tasks are misrouted to the code lane and die in FIX/BLOCK (or produce a code file for a docs ask). Router determinism is the substrate — keyword quality is the contract.
- suggested_fix: drop the bare `create the` from build (keep `create the app/service/api/cli/function/module/script` or require a second token), or add `create the readme` / `create the documentation` to document and rely on longer-keyword precedence.
- effort: S

## FINDING 6
- area: bench / slice-28 fallout
- severity: medium
- title: bench_nine.py is backend-blind — it FATALs on the missing Gemini key file even when the slice-28 tunnel key is provided, contradicting llm_provider.py's own "BENCH can still run in TESTING MODE" claim
- evidence: `bench/bench_nine.py`: `load_api_key()` reads ONLY `NINE_BENCH_KEY` → `~/.agent-vault/keys/gemini.key` and `sys.exit("FATAL: key file not found ...")`; `run_submit` force-injects `env["GEMINI_API_KEY"]` + `env["GEMINI_MODEL"]` and never touches NINE_LLM_BACKEND/NINE_LLM_API_KEY/OPENCODE_GO_API_KEY. `nine/runtime/llm_provider.py` docstring says: "While the Gemini quota is exhausted ... BENCH can still run in TESTING MODE by pointing the same model nodes at an OpenAI-compatible tunnel". Repro (hermetic, /tmp/repro_bench_key.py):
  ```
  NINE_LLM_BACKEND=openai  NINE_LLM_API_KEY=sk-tunnel-test-123  NINE_BENCH_KEY=/nonexistent/gemini.key
  SystemExit: FATAL: key file not found at /nonexistent/gemini.key ...
  ```
  The provided tunnel key is never consulted. Even with gemini.key present, the forced GEMINI_API_KEY/GEMINI_MODEL are ignored by the openai backend (api_key()/model_name() read the NINE_LLM_* chain), so the bench runs whatever the openai backend resolves (vault key if present) while its env claims Gemini.
- impact: the improvement loop (docs/IMPROVEMENT_LOOP.md runs bench/bench_nine.py as the core cycle) cannot run in the exact testing mode slice 28 was built for; operators get a Gemini-key FATAL with no hint that a valid tunnel key is available. Bench results under any mixed env are unreliable.
- suggested_fix: make load_api_key backend-aware: if NINE_LLM_BACKEND=openai, source NINE_LLM_API_KEY → OPENCODE_GO_API_KEY → vault opencode-go.key → auth.json (mirror llm_provider.api_key()); only require gemini.key on the gemini backend; log which backend/key source the bench run used.
- effort: S

## FINDING 7
- area: CLI/UX (slice-28 fallout)
- severity: low
- title: Model-or-fail errors say "requires GEMINI_API_KEY" even when the active backend is openai — the suggested fix cannot work (api_key() ignores GEMINI_API_KEY on the openai backend)
- evidence: all `_require_key` sites (transform_wf.py:24-29, debug_wf, build flagships, extract, research-quick/deep) raise `"<lane> requires GEMINI_API_KEY (ADK LlmAgent) - no offline fallback"` unconditionally, while `llm_provider.api_key()` for `NINE_LLM_BACKEND=openai` reads only NINE_LLM_API_KEY → OPENCODE_GO_API_KEY → vault → auth.json. Repro (hermetic, /tmp/repro_require_key_msg.py, HOME=empty temp dir):
  ```
  NINE_LLM_BACKEND=openai (no keys)
  WorkflowError -> transform (transform) requires GEMINI_API_KEY (ADK LlmAgent) ...
  ```
  Setting GEMINI_API_KEY per the message changes nothing on the openai backend. Note respond's message WAS updated ("GEMINI_API_KEY, or NINE_LLM_BACKEND=openai with an opencode key") — the ADK lanes were not.
- impact: an operator who followed the README provider-switch section ("set NINE_LLM_BACKEND=openai ... key from NINE_LLM_API_KEY -> ...") hits a dead-end error that points at the wrong key source; debugging requires reading llm_provider.py. Pure debuggability, but it is exactly the slice-28 surface the switch was supposed to document.
- suggested_fix: make the message backend-aware: on openai, say "requires an LLM key for the active backend (NINE_LLM_API_KEY / OPENCODE_GO_API_KEY / opencode-go vault)"; keep GEMINI_API_KEY wording only on the gemini backend.
- effort: S

## FINDING 8
- area: docs
- severity: low
- title: Demo/no-key and model-pinning claims are false post-slice-28: SUBMISSION "Try it (5 minutes, no key needed)" lists `nine chain flagship "build a calculator"` (requires a key, fails loud without one), and README "set GEMINI_MODEL to pin a different model" does not affect the ADK workflow nodes
- evidence: SUBMISSION.md "Try it (5 minutes, no key needed):" block lists `python demo.py ...` AND `nine chain flagship "build a calculator"`; flagship is 5 ADK model hops — without a key every hop raises WorkflowError (verified: `nine chain demo` runs offline, `nine chain flagship` cannot; responder/flagship `_require_key` fail-loud doctrine). README.md:16-18 "Model-agnostic by design ... set GEMINI_MODEL (and the matching API key) to pin a different model" — but every ADK node hardcodes `Gemini(model="gemini-3.6-flash")` (debug_wf, build_hop, research-quick, transform, compose), so GEMINI_MODEL only affects the raw-client nodes (responder, summarizer, router) and is IGNORED entirely on the openai backend (model_name() → NINE_LLM_MODEL). Also `nine chain demo "respond to customer refund question"` (README quickstart) SHIPs canned boilerplate ("Done: routed, executed, evidence-gated.") with no answer — the README presents it as a working answer path while T5-F2 deliberately keeps the demo lane out of production routing.
- impact: judges/operators following the submission quickstart without a key hit a hard failure on the flagship command ("5 minutes, no key needed" is false for 2 of 3 listed commands); anyone trying to pin a different Gemini model via GEMINI_MODEL gets it only in the router/responder, not the actual ADK build/debug lanes — silent half-pinning.
- suggested_fix: SUBMISSION: replace the flagship line with `nine chain demo "respond to a task"` or add "requires an API key"; README: state that GEMINI_MODEL drives the raw-client nodes while ADK workflow nodes are pinned (or wire GEMINI_MODEL through the ADK node constructors), and add a one-line caveat that demo-lane output is canned boilerplate by design.
- effort: S
