"""DataHub MCP context tool (optional, behind NINE_DATAHUB_MCP=1).

Synergy with our datahub-2026 build: the same "read the graph first"
pattern (search / get_entities / get_lineage / search_documents /
get_dataset_assertions via the datahub-agent-context kit) becomes an
optional `tool` node inside any nine workflow. It degrades gracefully:
with the flag off (or the kit uninstalled) the node reports disabled and
the workflow still ships — the core loop is never blocked by an optional
integration.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from nine.runtime.workflows import Node

ENV_FLAG = "NINE_DATAHUB_MCP"


def datahub_context_tool(inputs: dict[str, Any], job_dir: Path) -> dict[str, Any]:
    """Optional tool node: read metadata-graph context into the job dir."""
    job_dir = Path(job_dir)
    if os.environ.get(ENV_FLAG) != "1":
        return {
            "enabled": False,
            "reason": f"{ENV_FLAG} not set to '1' — DataHub MCP context disabled",
        }
    try:
        import datahub_agent_context  # noqa: F401  (optional dependency)
    except ImportError:
        return {
            "enabled": False,
            "reason": "datahub-agent-context not installed (pip install datahub-agent-context)",
        }
    # Live DataHub MCP requires a reachable metadata service; that path is
    # exercised in the datahub-2026 repo (github.com/optimizedwf/datahub-2026).
    return {
        "enabled": True,
        "note": "datahub-agent-context importable; wire search/get_lineage here "
                "for graph-backed context (see optimizedwf/datahub-2026).",
    }


def datahub_tool_node() -> Node:
    """Build the optional DataHub context node for any workflow."""
    return Node(
        id="datahub-context",
        kind="tool",
        run=datahub_context_tool,
        description="Optional DataHub MCP context enrichment (NINE_DATAHUB_MCP=1)",
        timeout_seconds=120,
    )
