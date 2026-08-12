"""Google ADK 2.0 integration — the Google agent framework layer.

chow-lite is built ON Google ADK 2.0 (the required "agent framework" for
the hackathon). This module adapts ADK agents into chow-lite workflow nodes:

    * an ADK agent (with tools, sessions, memory) can be a `subagent` node
    * the workflow engine drives ADK agents with real tool calls
    * sessions/memory map to the chow-lite job ledger (durable state)
    * ADK evaluate maps to the chow-lite evidence gate

ADK primitives used (google.github.io/adk-docs):
    agents/routing          -> chow-lite Router
    agents/workflow-agents  -> chow-lite WorkflowExecutor
    sessions/memory         -> job ledger + persistent state
    evaluate                -> evidence gate
    deploy/cloud-run        -> deploy/cloud-run.yaml

This module imports ADK lazily so the core (router/ledger/gates) works
without ADK installed — useful for CI and tests.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Optional


def adk_available() -> bool:
    try:
        import google.adk  # noqa: F401
        return True
    except ImportError:
        return False


def make_adk_node(
    agent: Any,
    description: str = "ADK agent step",
) -> dict[str, Any]:
    """Wrap an ADK Agent as a chow-lite workflow node spec.

    The returned dict can be turned into a Node with kind='subagent'
    whose run() drives the ADK agent synchronously (invoke once,
    collect final message + tool calls as evidence).
    """
    def _run(inputs: dict[str, Any], job_dir: Path) -> dict[str, Any]:
        task = inputs.get("task", "")
        # ADK 2.0 synchronous invoke: run(agent, task) -> AsyncResponse
        import google.adk  # noqa: F401
        from google.adk.runners import InMemoryRunner  # type: ignore

        runner = InMemoryRunner(agent=agent)
        result = runner.run(user_id="chow-lite", session_id="default", message=task)
        final = result.response if result.response is not None else None
        text = final.text if final is not None else ""
        tool_calls = list(getattr(result, "tool_calls", []) or [])
        return {
            "output": text,
            "tool_calls": [str(t) for t in tool_calls],
            "artifact": None,
        }

    return {
        "id": agent.name if hasattr(agent, "name") else "adk-node",
        "kind": "subagent",
        "run": _run,
        "description": description,
    }


def register_adk_agents(router: Any, agents: list[Any]) -> None:
    """Register ADK agents into the chow-lite router catalog."""
    for agent in agents:
        name = agent.name if hasattr(agent, "name") else "adk-agent"
        desc = getattr(agent, "description", "") or "ADK agent"
        router.register(workflow_id=name, keywords=[name.lower()], description=desc)
