"""Google ADK 2.0 integration — the Google agent framework layer.

nine is built ON Google ADK 2.0 (the required "agent framework" for
the hackathon). This module adapts ADK agents into nine workflow nodes:

    * an ADK agent (with tools, sessions, memory) can be a `subagent` node
    * ADK sessions/memory map to the nine job ledger (durable state)
    * ADK evaluate maps to the nine evidence gate
    * ADK observability traces are emitted per job

Verified against google-adk 2.6.3 (2026-08-12):
    agents/routing          -> nine Router
    agents/workflow-agents  -> nine WorkflowExecutor
    sessions/memory         -> job ledger + persistent state
    evaluate                -> evidence gate
    deploy/cloud-run        -> deploy/cloud-run.yaml

ADK is imported lazily so the core (router/ledger/gates) works without ADK
installed (dependency adapter — ADKAgentNode still raises on agent failure;
it never fabricates a SHIP). No model-output fallbacks exist anywhere in
nine: a missing dependency or key fails loud.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

_BRACE_NEUTRAL_RE = re.compile(r"\{+")


def _neutralize_instruction_braces(text: str) -> str:
    """Neutralize `{identifier}` sequences that ADK's instruction template
    engine would try to interpolate as session-state variables.

    google-adk's `inject_session_state` (instructions_utils.py) scans the
    agent instruction for `{+[^{}]*}+` and replaces each match whose inner
    name is a valid Python identifier with the session state value —
    raising KeyError when the variable is absent. The debug workflow's
    instructions embed the model's own ROOT_CAUSE.md / code snippets, and
    an f-string placeholder like `{stripped}` inside the embedded code
    crashes the instruction provider BEFORE any LLM call, surfacing as an
    instant "empty stream" (0 HTTP calls, 0.5s per retry).

    Insert a zero-width space after the first `{` of each brace group:
    `{stripped}` -> `{\u200bstripped}`. The inner name is then no longer a
    valid identifier, so ADK passes the match through unchanged. The
    invisible character is never emitted by the model (it copies the
    *text* it sees) and Python's tokenizer ignores it, so embedded code
    stays executable.
    """
    return _BRACE_NEUTRAL_RE.sub(lambda m: m.group(0)[0] + "\u200b" + m.group(0)[1:], text)


def adk_available() -> bool:
    try:
        import google.adk  # noqa: F401
        return True
    except ImportError:
        return False


class ADKAgentNode:
    """Adapts a Google ADK 2.0 LlmAgent into a nine workflow node.

    The node runs the ADK agent via InMemoryRunner (sync convenience API),
    collects the final response text + all function calls as evidence, and
    returns a dict the WorkflowExecutor can register as artifacts.
    """

    # Backoff between empty-stream retry attempts (ADK swallows Gemini
    # free-tier 429s into empty streams). Tests zero this to stay hermetic.
    _empty_backoff_s: float = 3.0

    def __init__(self, agent: Any, app_name: str = "nine") -> None:
        # testing mode: register the OpenAI-compatible ADK LLM so this node's
        # LlmAgent resolves to the tunnel (DS4 Flash) instead of Gemini.
        from nine.runtime import llm_provider

        llm_provider.install_adk_override()

        from google.adk.runners import InMemoryRunner

        # ADK interpolates {var} in instructions as session state (KeyError
        # when absent). Neutralize braces in embedded code/ROOT_CAUSE text
        # so the instruction provider can never crash before the LLM call
        # (slice-41: `{stripped}` in ROOT_CAUSE.md -> instant empty stream
        # on every patch attempt, 0 HTTP calls).
        inst = getattr(agent, "instruction", None)
        if isinstance(inst, str):
            agent.instruction = _neutralize_instruction_braces(inst)
        self.agent = agent
        self.app_name = app_name
        self.runner = InMemoryRunner(agent=agent, app_name=app_name)
        self._created_sessions: set[str] = set()
        # guarantee a UNIQUE session id per __call__ even if two attempts of
        # the same job land in the same clock tick (ms-granular monotonic
        # stamp collided -> session silently REUSED -> retry inherited the
        # prior conversation; caught as a flaky session-count test).
        self._attempt_seq = 0

    def _ensure_session(self, user_id: str, session_id: str) -> None:
        # sessions are per-job; track created ids (a single bool previously
        # skipped session creation for the 2nd job -> SessionNotFoundError)
        if session_id in self._created_sessions:
            return
        # create_session is a coroutine on google-adk 2.6.x
        asyncio.run(
            self.runner.session_service.create_session(
                app_name=self.app_name, user_id=user_id, session_id=session_id
            )
        )
        self._created_sessions.add(session_id)

    def __call__(self, inputs: dict[str, Any], job_dir: Path) -> dict[str, Any]:
        from google.genai import types

        task = inputs.get("task", "")
        user_id = "nine"
        # Session scope: FRESH PER ATTEMPT. A chain (research->plan->build)
        # shares the chain job_id, and retries of a hop would append to the
        # same session — on a small local model (qwen3:8b, 8192 ctx) the
        # growing conversation overflows and Ollama context-shifts (drops
        # the oldest 4K tokens), which re-evaluates and loops for 10+
        # minutes (seen in slice-40: 500s after 10m with context shifts).
        # A fresh session per attempt keeps every model turn small and
        # deterministic: retries redo the hop from zero (correct for a
        # FIX directive, which is reworked input anyway).
        agent_name = getattr(self.agent, "name", "agent")
        # monotonic_ns + per-node sequence: unique per attempt, never
        # collides across rapid successive calls on the same node. Lazy
        # getattr so object.__new__-constructed test nodes work too.
        seq = getattr(self, "_attempt_seq", 0)
        self._attempt_seq = seq + 1
        attempt_stamp = f"{time.monotonic_ns()}-{seq}"
        session_id = f"job-{inputs.get('job_id', 'default')}-{agent_name}-{attempt_stamp}"
        self._ensure_session(user_id, session_id)

        # sync run(): local-testing convenience API that drains the async
        # generator for us (verified on google-adk 2.6.3). Retry transient
        # quota/availability errors (429/503 are normal on Gemini free tier).
        # NOTE: ADK swallows Gemini free-tier 429s into an EMPTY stream (no
        # exception), so an empty stream is ALSO retried with backoff — the
        # gem-r1 bench (slice 38) showed 5/10 fixtures ERRORing because each
        # empty stream raised immediately and node-level retries re-hit the
        # same rate limit within seconds.
        events: list[Any] = []
        last_exc: Exception | None = None
        empty_attempts = 0
        # RunConfig cap: a small/local model can loop on a tool (re-writing
        # the same file) and burn the node deadline turn after turn. Bound
        # the LLM calls per agent run — default 24 (a multi-file build hop
        # needs ~8-12 calls: solution + tests + verification turns);
        # NINE_MAX_LLM_CALLS overrides.
        from google.adk.agents import RunConfig

        try:
            _max_calls = int(os.environ.get("NINE_MAX_LLM_CALLS", "24"))
        except ValueError:
            # torture-24 F5: a non-numeric value silently fell back to the
            # 24 default while the <1 branch below warned loudly — a
            # typo'd NINE_MAX_LLM_CALLS=l4 was invisible to the operator
            # (the cap they believed they tightened never applied). Mirror
            # the established junk-env convention (T9-F6, T22-F2): surface
            # it once on stderr.
            _raw = os.environ.get("NINE_MAX_LLM_CALLS", "24")
            print("WARNING: NINE_MAX_LLM_CALLS not an integer "
                  f"(got {_raw!r}); using 24", file=sys.stderr)
            _max_calls = 24
        if _max_calls < 1:
            # torture-21 F4: 0/negative silently DISABLES the budget in ADK
            # ("no enforcement ... never ending communication between the
            # model and the agent") - the exact runaway the cap exists to
            # prevent. Range-validate like the node-timeout override.
            print("WARNING: NINE_MAX_LLM_CALLS must be >= 1 "
                  f"(got {_max_calls}); using 24", file=sys.stderr)
            _max_calls = 24
        for attempt in range(3):
            try:
                evs = list(
                    self.runner.run(
                        user_id=user_id,
                        session_id=session_id,
                        new_message=types.Content(
                            role="user",
                            parts=[types.Part.from_text(text=task)],
                        ),
                        run_config=RunConfig(max_llm_calls=_max_calls),
                    )
                )
                if evs:
                    events = evs
                    break
                empty_attempts += 1
                if attempt == 2:
                    break
                time.sleep(self._empty_backoff_s * (attempt + 1))
            except Exception as exc:  # noqa: BLE001 - transient API errors
                from google.adk.agents.invocation_context import (
                    LlmCallsLimitExceededError,
                )

                if isinstance(exc, LlmCallsLimitExceededError):
                    # The run_config budget was exhausted (small-model loop).
                    # Don't burn retries on a budget that won't grow — fail
                    # loud with the real cause instead of an empty-stream
                    # hint that misleads (Gemini 429 vs budget are different).
                    raise RuntimeError(
                        f"ADK agent exceeded max_llm_calls={_max_calls} for "
                        f"task: {task[:120]!r} (model looped on tools) — "
                        "raise NINE_MAX_LLM_CALLS or tighten the agent "
                        "instruction"
                    ) from exc
                last_exc = exc
                if attempt == 2:
                    break
                time.sleep(2.0 * (attempt + 1))
        if last_exc is not None and not events:
            # surface the error so the evidence gate sees a FIX/BLOCK,
            # never a fabricated SHIP
            raise last_exc

        final_text = ""
        function_calls: list[str] = []
        for ev in events:
            if ev.content and ev.content.parts:
                for part in ev.content.parts:
                    if part.function_call is not None:
                        function_calls.append(
                            f"{part.function_call.name}({json.dumps(part.function_call.args)})"
                        )
            if bool(ev.is_final_response) and ev.content and ev.content.parts:
                texts = [p.text for p in ev.content.parts if p.text]
                if texts:
                    final_text = texts[0]

        if not events or not (final_text or function_calls):
            # An empty agent stream is NOT success: ADK yields an empty
            # stream (no text, no tool calls) instead of raising on Gemini
            # free-tier quota exhaustion (429 RESOURCE_EXHAUSTED) or an
            # unhelpful model turn. Treat it as a retryable failure so the
            # executor retries and then fails LOUD — never a silent pass
            # that SHIPs the unmodified artifact or burns fix loops.
            hint = f" (last error: {last_exc})" if last_exc is not None else ""
            if empty_attempts:
                hint += f" (empty stream x{empty_attempts} with {self._empty_backoff_s:g}s backoff)"
            raise RuntimeError(
                f"ADK agent produced no output for task: {task[:120]!r}{hint} "
                "- empty stream (often Gemini 429 quota exhaustion; retry "
                "after quota reset)"
            )

        # write the agent's output as an artifact for the evidence gate
        out_path = job_dir / "agent_output.md"
        out_path.write_text(
            f"# Agent output\n\n## Task\n{task}\n\n## Tool calls\n"
            + "\n".join(function_calls)
            + f"\n\n## Response\n{final_text}\n"
        )

        return {
            "output": final_text,
            "function_calls": function_calls,
            "artifact_path": str(out_path),
        }


def make_adk_node(agent: Any, description: str = "ADK agent step") -> dict[str, Any]:
    """Return a nine Node spec (kind='subagent') wrapping an ADK agent."""
    adapter = ADKAgentNode(agent)
    name = getattr(agent, "name", "adk_node")
    return {
        "id": name,
        "kind": "subagent",
        "run": adapter,
        "description": description,
    }


def register_adk_agents(router: Any, agents: list[Any]) -> None:
    """Register ADK agents into the nine router catalog."""
    for agent in agents:
        name = getattr(agent, "name", "adk_agent")
        desc = getattr(agent, "description", "") or "ADK agent"
        router.register(workflow_id=name, keywords=[name.lower()], description=desc)
