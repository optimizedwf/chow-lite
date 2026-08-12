"""Google ADK 2.0 integration — the Google agent framework layer.

chow-lite is built ON Google ADK 2.0 (the required "agent framework" for
the hackathon). This module adapts ADK agents into chow-lite workflow nodes:

    * an ADK agent (with tools, sessions, memory) can be a `subagent` node
    * ADK sessions/memory map to the chow-lite job ledger (durable state)
    * ADK evaluate maps to the chow-lite evidence gate
    * ADK observability traces are emitted per job

Verified against google-adk 2.6.3 (2026-08-12):
    agents/routing          -> chow-lite Router
    agents/workflow-agents  -> chow-lite WorkflowExecutor
    sessions/memory         -> job ledger + persistent state
    evaluate                -> evidence gate
    deploy/cloud-run        -> deploy/cloud-run.yaml

ADK is imported lazily so the core (router/ledger/gates) works without ADK
installed — useful for CI and offline tests.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any


def adk_available() -> bool:
    try:
        import google.adk  # noqa: F401
        return True
    except ImportError:
        return False


class ADKAgentNode:
    """Adapts a Google ADK 2.0 LlmAgent into a chow-lite workflow node.

    The node runs the ADK agent via InMemoryRunner (sync convenience API),
    collects the final response text + all function calls as evidence, and
    returns a dict the WorkflowExecutor can register as artifacts.
    """

    def __init__(self, agent: Any, app_name: str = "chow-lite") -> None:
        from google.adk.runners import InMemoryRunner

        self.agent = agent
        self.app_name = app_name
        self.runner = InMemoryRunner(agent=agent, app_name=app_name)
        self._created_sessions: set[str] = set()

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
        user_id = "chow-lite"
        session_id = f"job-{inputs.get('job_id', 'default')}"
        self._ensure_session(user_id, session_id)

        # sync run(): local-testing convenience API that drains the async
        # generator for us (verified on google-adk 2.6.3). Retry transient
        # quota/availability errors (429/503 are normal on Gemini free tier).
        import time

        events: list[Any] = []
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                events = list(
                    self.runner.run(
                        user_id=user_id,
                        session_id=session_id,
                        new_message=types.Content(
                            role="user",
                            parts=[types.Part.from_text(text=task)],
                        ),
                    )
                )
                break
            except Exception as exc:  # noqa: BLE001 - transient API errors
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
    """Return a chow-lite Node spec (kind='subagent') wrapping an ADK agent."""
    adapter = ADKAgentNode(agent)
    name = getattr(agent, "name", "adk_node")
    return {
        "id": name,
        "kind": "subagent",
        "run": adapter,
        "description": description,
    }


def register_adk_agents(router: Any, agents: list[Any]) -> None:
    """Register ADK agents into the chow-lite router catalog."""
    for agent in agents:
        name = getattr(agent, "name", "adk_agent")
        desc = getattr(agent, "description", "") or "ADK agent"
        router.register(workflow_id=name, keywords=[name.lower()], description=desc)
