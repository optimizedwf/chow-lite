"""Extract workflow - unstructured source -> structured JSON (OUTPUT.json).

The `extract` lane of nine: read-source (bash) collects the workspace
source into SOURCE.md; extractor (tool/ADK) reads it plus the task (which
may name the target schema) and writes OUTPUT.json as valid JSON via the
write_file tool. Gate requires OUTPUT.json to parse as JSON and carry at
least one key.

Model-or-fail: without GEMINI_API_KEY the extractor raises WorkflowError -
the job fails loud. NEVER a canned/empty JSON stub.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from nine.chains.chain import Hop
from nine.gates.evidence import (
    exit_codes_check,
    required_artifact_check,
)
from nine.runtime.workflows import Node, Workflow, WorkflowError


def _read_source_command() -> str:
    """Bash node: collect the workspace source into SOURCE.md.

    Reads solution.py (or the solution/ tree) into SOURCE.md with a file
    inventory header. Always exits 0; evidence is the artifact.
    """
    return r"""
echo '# Source (for extraction)' > SOURCE.md
echo '## Inventory' >> SOURCE.md
if [ -f solution.py ]; then
  echo '- solution.py (top-level module)' >> SOURCE.md
  echo '## solution.py' >> SOURCE.md
  cat solution.py >> SOURCE.md
elif [ -d solution ]; then
  find solution -name '*.py' -not -path '*/__pycache__/*' | sort | while read -r f; do
    echo "- $f" >> SOURCE.md
  done
  echo '## Sources' >> SOURCE.md
  for f in $(find solution -name '*.py' -not -path '*/__pycache__/*' | sort); do
    echo "### $f" >> SOURCE.md
    cat "$f" >> SOURCE.md
  done
else
  echo '- (no source files found)' >> SOURCE.md
fi
exit 0
"""


def _extractor_adk_node() -> Node:
    """ADK LlmAgent: extract structured JSON -> OUTPUT.json.

    Model-or-fail: raises WorkflowError without GEMINI_API_KEY.
    """
    def _run(inputs: dict, job_dir) -> dict:
        from nine.runtime.adk_runtime import ADKAgentNode

        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:500]
        if not os.environ.get("GEMINI_API_KEY"):
            raise WorkflowError(
                "extract (extractor) requires GEMINI_API_KEY (ADK "
                "LlmAgent) - no offline fallback, nine is model-driven"
            )

        from google.adk.agents import LlmAgent
        from google.adk.models import Gemini
        from google.adk.tools import FunctionTool

        def write_file(path: str, content: str) -> str:
            """Write a file into the workspace (job dir)."""
            (job_dir / path).write_text(content, encoding="utf-8")
            return f"wrote {path} ({len(content)} bytes)"

        source = ""
        if (job_dir / "SOURCE.md").exists():
            source = (job_dir / "SOURCE.md").read_text(
                encoding="utf-8")[:6000]
        else:
            source = "(no source found)"

        agent = LlmAgent(
            name="extractor",
            model=Gemini(model="gemini-3.6-flash"),
            instruction=(
                "You are the extractor of nine, an evidence-gated agent "
                "OS. Extract structured data from the source below into "
                "valid JSON and write it to OUTPUT.json using the "
                "write_file tool.\n"
                "Rules:\n"
                "- Output ONLY a JSON value (object or array) - no markdown "
                "fences, no commentary inside the file.\n"
                "- Follow the target schema in the task if one is given; "
                "otherwise design a sensible schema that captures the "
                "source's key facts (names, signatures, fields, constants, "
                "relationships).\n"
                "- Every value must come from the source - never invent "
                "data.\n"
                "- The file must parse with json.loads.\n"
                f"Task: {task}\n"
                f"Source:\n```\n{source}\n```"
            ),
            tools=[FunctionTool(write_file)],
        )
        return ADKAgentNode(agent)(inputs, job_dir)

    return Node(
        id="extractor", kind="tool", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="ADK LlmAgent writes OUTPUT.json (fails loud without a model)",
    )


def _valid_json_check(ctx: dict[str, Any], workdir: Path) -> tuple[bool, str]:
    """OUTPUT.json must parse as JSON and carry at least one key."""
    p = Path(workdir) / "OUTPUT.json"
    if not p.exists():
        return False, "OUTPUT.json missing"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return False, f"OUTPUT.json is not valid JSON: {exc}"
    if isinstance(data, dict):
        if not data:
            return False, "OUTPUT.json is an empty object"
        return True, f"OUTPUT.json valid JSON object with {len(data)} keys"
    if isinstance(data, list):
        if not data:
            return False, "OUTPUT.json is an empty array"
        return True, f"OUTPUT.json valid JSON array with {len(data)} items"
    return False, "OUTPUT.json must be a JSON object or array"


def extract_hop() -> Hop:
    """The `extract` workflow: unstructured -> structured JSON.

    Two-node hop:
      1. read-source (bash)  - SOURCE.md (workspace source)
      2. extractor (tool/ADK) - OUTPUT.json (valid structured JSON)

    Gate: OUTPUT.json valid JSON with >= 1 key + artifacts + exit codes.
    """
    wf = Workflow(id="extract",
                  description="Unstructured source -> structured JSON")
    read_source = Node(id="read-source", kind="bash",
                       command=_read_source_command(),
                       description="Collect source into SOURCE.md")
    extractor = _extractor_adk_node()
    extractor.depends_on = ["read-source"]
    wf.add_node(read_source)
    wf.add_node(extractor)
    return Hop(
        id="extract", workflow=wf,
        required_artifacts=["SOURCE.md", "OUTPUT.json"],
        gate_checks={
            "exit-codes": exit_codes_check(),
            "artifacts": required_artifact_check(["SOURCE.md", "OUTPUT.json"]),
            "valid-json": _valid_json_check,
        },
        max_fix_loops=2,
    )
