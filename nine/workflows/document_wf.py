"""Document workflow - docgen for a codebase: README + API doc.

The `document` lane of nine: an inventory bash node maps the workspace
(INVENTORY.md: file tree + line counts + entrypoints + test layout), then an
ADK LlmAgent docgen node reads the inventory + source and writes README.md
(overview, install, usage) and API.md (public functions/classes/signatures).

Model-or-fail: without GEMINI_API_KEY the docgen node raises WorkflowError -
the job fails loud. NEVER a canned README.
"""
from __future__ import annotations

import os
from pathlib import Path

from nine.chains.chain import Hop
from nine.gates.evidence import (
    exit_codes_check,
    required_artifact_check,
)
from nine.runtime.fsafety import contained_write
from nine.runtime.workflows import Node, Workflow, WorkflowError


def _inventory_command() -> str:
    """Bash node: map the workspace and write INVENTORY.md.

    Records the file tree (with sizes + line counts for source files),
    flags entrypoints (main.py, solution.py) and test layouts, and detects
    a solution/ project dir. Always exits 0; evidence is the artifact.
    """
    return (
        "echo '# Codebase Inventory' > INVENTORY.md; "
        "echo '## File Tree' >> INVENTORY.md; "
        "ls -la >> INVENTORY.md; "
        "echo '## Source Line Counts' >> INVENTORY.md; "
        "find . -name '*.py' -not -path './.venv/*' -not -path './__pycache__/*' "
        "| while read -r f; do wc -l '$f'; done >> INVENTORY.md; "
        "echo '## Entrypoints' >> INVENTORY.md; "
        "if [ -f solution.py ]; then echo '- solution.py (top-level)' >> INVENTORY.md; fi; "
        "if [ -f solution/main.py ]; then echo '- solution/main.py' >> INVENTORY.md; fi; "
        "echo '## Tests' >> INVENTORY.md; "
        "if [ -f test_solution.py ]; then echo '- test_solution.py' >> INVENTORY.md; fi; "
        "if ls solution/test_*.py >/dev/null 2>&1; then "
        "  ls solution/test_*.py | sed 's/^/- /' >> INVENTORY.md; "
        "fi; "
        "exit 0"
    )


def _docgen_adk_node() -> Node:
    """ADK LlmAgent that writes README.md + API.md.

    Model-or-fail: raises WorkflowError without GEMINI_API_KEY.
    """
    def _run(inputs: dict, job_dir) -> dict:
        from nine.runtime.adk_runtime import ADKAgentNode

        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:500]
        if not os.environ.get("GEMINI_API_KEY", "").strip():
            raise WorkflowError(
                "document (docgen) requires GEMINI_API_KEY (ADK LlmAgent) - "
                "no offline fallback, nine is model-driven"
            )

        from google.adk.agents import LlmAgent
        from google.adk.models import Gemini
        from google.adk.tools import FunctionTool

        def write_file(path: str, content: str) -> str:
            """Write a doc file into the workspace (job dir)."""
            contained_write(job_dir, path, content)
            return f"wrote {path} ({len(content)} bytes)"

        inventory = ""
        if (job_dir / "INVENTORY.md").exists():
            inventory = (job_dir / "INVENTORY.md").read_text(
                encoding="utf-8")[:3000]

        # Pull representative source for the docgen model.
        sources = ""
        if (job_dir / "solution.py").exists():
            sources = (job_dir / "solution.py").read_text(
                encoding="utf-8")[:4000]
        else:
            sol = job_dir / "solution"
            if sol.is_dir():
                parts = []
                for p in sorted(sol.rglob("*.py")):
                    if "__pycache__" in str(p):
                        continue
                    parts.append(
                        f"### {p.relative_to(job_dir)}\n"
                        + p.read_text(encoding="utf-8", errors="replace")[:2000]
                    )
                sources = "\n".join(parts)[:6000]

        agent = LlmAgent(
            name="docgen",
            model=Gemini(model="gemini-3.6-flash"),
            instruction=(
                "You are the docgen of nine, an evidence-gated agent OS. "
                "Write two documentation files for the codebase using the "
                "write_file tool:\n"
                "1. README.md - project overview, what it does, how to run "
                "it (entrypoint), and how to run tests. Concise, accurate, "
                "no invented features.\n"
                "2. API.md - every public function/class: name, signature, "
                "one-line purpose. List what is actually in the code.\n"
                "Write both files completely; they must reflect the "
                "inventory and source you were given.\n"
                f"Task: {task}\n"
                f"INVENTORY.md:\n{inventory}\n"
                f"Source:\n```python\n{sources}\n```"
            ),
            tools=[FunctionTool(write_file)],
        )
        node = ADKAgentNode(agent)
        return node(inputs, job_dir)

    return Node(
        id="docgen", kind="tool", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="ADK LlmAgent writes README.md + API.md (fails loud without a model)",
    )


def document_hop() -> Hop:
    """The `document` workflow: docgen for a codebase.

    Two-node hop:
      1. inventory (bash)   - maps workspace -> INVENTORY.md
      2. docgen (tool/ADK)  - reads inventory + source, writes README.md + API.md

    Gate: README.md + API.md (+ INVENTORY.md) present, EVAL.json passed,
    all bash nodes exited 0. A missing doc artifact triggers a FIX loop.
    """
    wf = Workflow(id="document", description="Docgen for a codebase: README + API doc")
    inventory = Node(id="inventory", kind="bash", command=_inventory_command(),
                     description="Map workspace -> INVENTORY.md")
    docgen = _docgen_adk_node()
    docgen.depends_on = ["inventory"]
    wf.add_node(inventory)
    wf.add_node(docgen)
    return Hop(
        id="document", workflow=wf,
        required_artifacts=["INVENTORY.md", "README.md", "API.md"],
        gate_checks={
            "exit-codes": exit_codes_check(),
            "artifacts": required_artifact_check(
                ["INVENTORY.md", "README.md", "API.md"]
            ),
        },
        max_fix_loops=2,
    )
