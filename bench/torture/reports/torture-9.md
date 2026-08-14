# TORTURE-9 — LLM Provider Switch + Model Layer (slice 28)

**Repo**: chow-lite @ 59bc4cf · **HEAD**: "slice 28: LLM provider switch - NINE_LLM_BACKEND=openai routes model nodes to DS4 Flash tunnel for testing only (Gemini default unchanged)"
**Scope**: nine/runtime/llm_provider.py, nine/runtime/gemma.py, nine/runtime/responder.py, nine/runtime/summarizer.py, nine/cli.py, deploy/server.py, demo_live.py, nine/runtime/adk_runtime.py, nine/router/classifier.py, README provider-switch section.
**Method**: read-only; all repros run via `.venv/bin/python` with stubbed `requests.post` / local capture HTTP server / invented (fake) key values. No real keys were used or printed; no repo file modified.

7 findings: 1 critical, 3 high, 2 medium, 1 low.

---

## FINDING 1
- area: ADK override uninstall / registry restore
- severity: critical
- title: uninstall_adk_override() never restores Gemini — registry entries + lru_cache keep `_OpenAILlm` alive, so GEMINI_API_KEY is sent to the tunnel host after "restore"
- evidence: nine/runtime/llm_provider.py:353-355 (install) + 358-371 (uninstall); google.adk 2.6.3 `registry.py` `resolve()` is `@lru_cache(maxsize=32)` and iterates `_llm_registry_dict` in order with `re.fullmatch`.
  - `install_adk_override` calls `registry.LLMRegistry.register(_OpenAILlm)` (line 353), which inserts `_llm_registry_dict["^gemini-[0-9].*$"]` and `["^deepseek-v4-flash$"]` → `_OpenAILlm`. `uninstall` only pops the two *manual* keys `"gemini-.*"`/`"^gemini-.*$"` (lines 362-363) — the `register()` leftovers stay.
  - `resolve("gemini-3.6-flash")` is lru-cached during install; `uninstall` never calls `LLMRegistry.resolve.cache_clear()`.
  - Repro (`/tmp/repro_uninstall.py`): install → `resolve("gemini-3.6-flash")` → `_OpenAILlm`; uninstall → dict still contains `'^gemini-[0-9].*$': '_OpenAILlm'`, `'^deepseek-v4-flash$': '_OpenAILlm'`; `resolve()` AFTER uninstall still returns `_OpenAILlm`. Then flip env to `NINE_LLM_BACKEND=gemini` + `GEMINI_API_KEY=FAKE-GEMINI-SECRET-12345` (invented test value) and run the resolved class against a local capture server: the tunnel received `Authorization: Bearer FAKE-GEMINI-SECRET-12345`.
- impact: the "restore to Gemini" helper (and any in-process backend flip, e.g. a long-running server/test harness that toggles testing mode) silently keeps routing gemini ADK nodes to the OpenAI tunnel — and because `api_key()` on the gemini backend returns `GEMINI_API_KEY` (llm_provider.py:62-64), the real Gemini key is POSTed as a Bearer token to the third-party tunnel host (`NINE_LLM_BASE_URL` default https://opencode.ai/zen/go/v1). With real keys this is credential exfiltration; even without a key, "restored" nodes keep hitting the wrong model provider.
- suggested_fix: uninstall should (a) pop exactly the keys install added (`^gemini-[0-9].*$`, `^deepseek-v4-flash$`, `gemini-.*`, `^gemini-.*$`) or record the pre-install dict, (b) call `registry.LLMRegistry.resolve.cache_clear()`, (c) re-register lazy Gemini for every popped pattern. Regression test: install → resolve → uninstall → assert `resolve("gemini-3.6-flash")` is lazy Gemini AND that running it with `NINE_LLM_BACKEND=gemini` performs no HTTP POST (stub `requests.post` to raise on call).
- effort: S

## FINDING 2
- area: ADK override correctness (system instruction)
- severity: high
- title: ADK override silently drops LlmAgent's system instruction — tunnel payload has no system message, agents run without their instructions
- evidence: nine/runtime/llm_provider.py:243-281 `_messages_from` builds messages ONLY from `llm_request.contents`; the payload (293-297) never includes `config.system_instruction`. In google-adk 2.6.3, `LlmAgent.instruction` lands in `llm_request.config.system_instruction` (`LlmRequest.append_instructions`, llm_request.py; `DYNAMIC_INSTRUCTION_ROUTING` feature defaults to False — verified via `is_feature_enabled`).
  - Repro A (`/tmp/repro_sysinst3.py`): `LlmRequest(..., config.system_instruction="You are a market research agent...")` → stubbed tunnel receives roles `['user']`, no system message.
  - Repro B (e2e, `/tmp/repro_e2e.py`): real `LlmAgent(instruction="You are a market research agent. Use tools. Be concise.", tools=[FunctionTool(...)])` through `InMemoryRunner` with the override → first tunnel call payload is ONLY `{"role": "user", "content": "price?"}`. The instruction is gone.
- impact: in testing mode every ADK model node (research/plan/build/review hops, any LlmAgent) runs with NO system prompt. Instruction-driven behavior (output formats, tool-use mandates, no-boilerplate rules) is lost while the evidence gate still SHIPs the artifact (presence/non-empty checks pass) — nine ships work that never followed its instructions, silently.
- suggested_fix: in `generate_content_async`, prepend `{"role": "system", "content": cfg.system_instruction}` when `cfg.system_instruction` is set (and handle `contents` role=="system" Content as system, not user). Regression test: LlmRequest with `config.system_instruction` → assert payload[0] is the system message.
- effort: S

## FINDING 3
- area: ADK override correctness (tool-call round-trip)
- severity: high
- title: tool round-trips emit malformed messages — duplicate tool message on role="tool" contents, and a spurious empty user message between assistant tool_calls and the tool result on the real ADK path
- evidence: nine/runtime/llm_provider.py:275-280. The `elif role == "tool": out.extend(tool_msgs)` is followed by the unconditional trailing `if tool_msgs: out.extend(tool_msgs)` — same tool messages appended twice. For the real ADK path, function responses arrive as `Content(role="user", parts=[function_response])` (google-adk cli.py `run_in_process`: `types.Content(role='user', parts=[Part(function_response=...)])`), which hits the `else` branch (line 277-278) → emits `{"role": "user", "content": ""}` BEFORE the tool message.
  - Repro (`/tmp/repro_adk_override.py`): LlmRequest contents = [user, assistant w/ FunctionCall id=call_abc123, tool w/ FunctionResponse id=call_abc123] → payload messages end with TWO identical `{"role":"tool","tool_call_id":"call_abc123",...}` entries.
  - E2E (`/tmp/repro_e2e.py`): real LlmAgent+InMemoryRunner tool round-trip → second tunnel payload = [user, assistant(tool_calls call_1), **{"role":"user","content":""}**, tool(call_1)].
- impact: OpenAI-compatible endpoints require tool messages to immediately follow the assistant message with the matching tool_calls; the stray empty user message (and duplicate tool message on role="tool") is a protocol violation — strict servers 400 → tool calls fail in testing mode; at minimum every tool turn duplicates/wastes tokens and round-trips malformed history. The reference adapter in google-adk (`labs/openai/_openai_llm.py`) emits no such user message, so this is purely a nine bug.
- suggested_fix: restructure `_messages_from`: for a content whose parts are all function_response, emit ONLY the tool messages (no user message); delete the trailing `if tool_msgs: out.extend(tool_msgs)` block; skip appending empty-text user messages when the content carried only function_response parts. Regression test: assert the exact message sequence for a full tool round-trip (user → assistant+tool_calls → tool).
- effort: S

## FINDING 4
- area: dispatch rewires / registry takeover effectiveness
- severity: high
- title: ADK override is a no-op for the flagship chain — `Gemini(model=...)` instances bypass LLMRegistry, so testing mode still calls the real Gemini API
- evidence: google-adk 2.6.3 `LlmAgent.canonical_model`: `if isinstance(self.model, BaseLlm): return self.model` — instances never consult `LLMRegistry`. nine/chains/flagship.py:29/68/168/257 construct `LlmAgent(model=Gemini(model="gemini-3.6-flash"))` for the research/plan/build hops. `install_adk_override` only replaces registry entries (llm_provider.py:353-355).
  - Repro (`/tmp/repro_fleet_bypass3.py`): with `NINE_LLM_BACKEND=openai`, override installed, and a fake GEMINI_API_KEY set, replicate flagship's agent construction → `agent.canonical_model` is class `Gemini` (real API), NOT `_OpenAILlm`. The tunnel is never contacted for these nodes.
- impact: README "LLM provider switch (testing)" section claims the switch routes "the SAME model nodes (ADK LlmAgents, router, responder, summarizer, gemma teach hop) to an OpenAI-compatible tunnel" — false for the flagship chain that testing mode exists to unblock. With Gemini quota exhausted, flagship runs 429-fail; with quota present, it silently burns real Gemini budget while the operator believes they are on the tunnel. The self-test pin (`cli.py:594` `NINE_LLM_BACKEND=gemini`) can't detect this because no test asserts flagship agents resolve to the tunnel.
- suggested_fix: make the override also intercept instance-based resolution — e.g. `install_adk_override` additionally monkeypatches `LlmAgent.canonical_model` (or flagship builds `model="gemini-3.6-flash"` strings when `backend()=="openai"`). Regression test: with `NINE_LLM_BACKEND=openai`, `_research_adk_node()`'s agent `canonical_model` must be `_OpenAILlm`.
- effort: M

## FINDING 5
- area: model-or-fail honesty / artifacts & API truth
- severity: medium
- title: artifacts lie about which model served the job — RouteDecision.model hardcoded "gemini-3.6-flash", responder returns "gemini", /health reports GEMINI_MODEL on the openai backend
- evidence:
  - nine/router/classifier.py:237 `model_used = "gemini-3.6-flash"` unconditionally when any model routed. Repro (`/tmp/repro_model_lies.py`): `NINE_LLM_BACKEND=openai` + stub tunnel → `build_default_router().classify(...)` returns `decision.model == "gemini-3.6-flash"` while `llm_provider.model_name() == "deepseek-v4-flash"`. The decision is persisted in the ledger (`ledger.attach_route_decision`) and surfaced via `nine status` / `GET /v1/jobs` / route events.
  - nine/runtime/responder.py:78 returns `model_used="gemini"` hardcoded — ignores `GEMINI_MODEL` (repro: with `GEMINI_MODEL=gemini-2.5-flash`, responder reports "gemini").
  - deploy/server.py:131,314 `/health` reports `MODEL = GEMINI_MODEL` even when `NINE_LLM_BACKEND=openai` actually serves deepseek-v4-flash.
  - Doc-truth gap: tests/test_torture_harvest_5.py `test_no_stale_gemini_35_flash_claims` checks gemma.py/flagship.py/classifier.py/server.py but not demo_live.py, which still claims "real Gemini 3.5 Flash call" (demo_live.py:4,52).
- impact: the ledger, job API, health endpoint and route events misreport the serving model — audit/observability lies; anyone correlating outputs with models (cost, quality) is misled; README's "model-agnostic" story contradicted by hardcoded identities.
- suggested_fix: use `llm_provider.model_name()` for RouteDecision.model (classifier gets the model name from the adapter), responder, and /health; include demo_live.py in the doc-truth test list. Regression test: openai backend → `decision.model == "deepseek-v4-flash"`.
- effort: S

## FINDING 6
- area: backend edge cases (junk NINE_LLM_BACKEND)
- severity: medium
- title: junk NINE_LLM_BACKEND values silently fall back to the gemini backend — a typo silently burns real Gemini quota (or fails with misleading GEMINI_API_KEY errors) in testing mode
- evidence: nine/runtime/llm_provider.py:33-41 — `backend()` maps anything not in ("openai","opencode","rue") to "gemini", including typos like `openaai`, `openai-tunnel`, `OpenAI`. tests/test_llm_provider.py:42-44 `test_backend_junk_falls_back_to_gemini` codifies the silent fallback as intended.
  - Scenario: operator runs testing mode to avoid exhausted Gemini quota, types `NINE_LLM_BACKEND=openai ` (trailing space is fine) or `NINE_LLM_BACKEND=OPENAI_TUNNEL` → `backend()=="gemini"`. If `GEMINI_API_KEY` is set (normal operator env), nine silently routes all model nodes to real Gemini — spending real quota/cost while the operator believes the tunnel is active. If not set, `respond_text` raises "respond requires an LLM key (GEMINI_API_KEY, or NINE_LLM_BACKEND=openai with an opencode key)" (responder.py:41-45) — misleading for someone who *did* configure the tunnel.
- impact: silent wrong-backend execution → wasted model budget / unexpected real-Gemini spend in "testing" runs, or confusing failure modes; contradicts the docstring's "Backend selection is a pure function of the environment" (a typo is not the gemini default).
- suggested_fix: reject unknown values loudly — `backend()` should raise (or log a prominent warning listing valid values) for non-empty values outside the known set instead of silently returning "gemini". Regression test: `NINE_LLM_BACKEND=openaai` raises/records a warning, and `key_available()` stays honest about which backend is active.
- effort: S

## FINDING 7
- area: ADK override error path / key guard consistency
- severity: low
- title: ADK override POSTs without checking api_key() and uses finish_reason="ERROR", which is not a valid types.FinishReason (warning + guard bypass); stream=True accepted but ignored
- evidence:
  - nine/runtime/llm_provider.py:307-312 `_OpenAILlm.generate_content_async` POSTs unconditionally (Bearer `api_key()` may be empty) — unlike `chat_text` which refuses without a key (llm_provider.py:92-93). Repro: no key set → POST still attempted with `Authorization: Bearer `.
  - llm_provider.py:316,325,348 `finish_reason="ERROR"` — `types.FinishReason` has no ERROR member; genai's CaseInSensitiveEnum `_missing_` emits `UserWarning: ERROR is not a valid FinishReason` on EVERY non-200/exception (repro `/tmp/repro_error_finish.py`) and creates an unknown enum member, so ADK's empty-content STOP guard (`base_llm_flow.py` `_postprocess_async`) never fires for ERROR responses.
  - llm_provider.py:290 `stream` param is accepted but ignored — always a single non-streamed POST yielding one full response.
- impact: warning noise per failure, empty-auth requests to the tunnel when a key vanishes mid-run, shallow streaming support (a stream=True caller gets one complete chunk, which mostly works but is not SSE).
- suggested_fix: guard `if not api_key(): yield ERROR response and return` before POST (mirror chat_text); keep "ERROR" only if the installed genai version defines it, otherwise map to a valid FinishReason (e.g. `FinishReason.SAFETY` is wrong semantically — prefer raising/turn_complete) or silence the enum warning by constructing via the class member path; document/assert `stream` unsupported. Regression test: no key → `requests.post` must not be called.
- effort: S
