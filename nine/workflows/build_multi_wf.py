"""Build-multi workflow - multi-file project scaffold via ADK.

This is the `build-multi` lane of nine: given a task (and optionally a
PLAN.md), an ADK LlmAgent writes a MULTI-FILE Python project under
solution/ (package modules, __init__.py, tests, entrypoint), then an
independent bash node verifies the project and writes EVAL.json.

Model-or-fail: without GEMINI_API_KEY the ADK node raises WorkflowError -
the job fails loud. NEVER a canned project.
"""
from __future__ import annotations

import os
from pathlib import Path

from nine.chains.chain import Hop
from nine.gates.evidence import (
    eval_json_check,
    exit_codes_check,
    required_artifact_check,
)
from nine.runtime.workflows import Node, Workflow


def _build_multi_adk_node() -> Node:
    """ADK LlmAgent that scaffolds a multi-file project under solution/.

    The write_file FunctionTool accepts paths with subdirectories
    (e.g. solution/main.py), so the agent can produce a real project
    layout: package modules, __init__.py, optional tests, entrypoint.

    Model-or-fail: raises WorkflowError without GEMINI_API_KEY.
    """
    from nine.runtime.workflows import WorkflowError

    def _run(inputs: dict, job_dir) -> dict:
        from nine.runtime.adk_runtime import ADKAgentNode

        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:1500]
        if not os.environ.get("GEMINI_API_KEY"):
            raise WorkflowError(
                "build-multi requires GEMINI_API_KEY (ADK LlmAgent) - no "
                "offline fallback, nine is model-driven"
            )

        from google.adk.agents import LlmAgent
        from google.adk.models import Gemini
        from google.adk.tools import FunctionTool

        def write_file(path: str, content: str) -> str:
            """Write a project file into the build workspace (job dir)."""
            target = job_dir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"wrote {path} ({len(content)} bytes)"

        plan = ""
        if (job_dir / "PLAN.md").exists():
            plan = (job_dir / "PLAN.md").read_text(encoding="utf-8")[:800]

        agent = LlmAgent(
            name="project-builder",
            model=Gemini(model="gemini-3.6-flash"),
            instruction=(
                "You are the build-multi hop of nine, an evidence-gated agent OS.\n"
                "Read the task and plan, then scaffold a MULTI-FILE Python "
                "project under the `solution/` directory using the write_file "
                "tool. The project must be a proper package: at least two "
                "modules (e.g. solution/main.py, solution/core.py), a "
                "solution/__init__.py exporting the public API using RELATIVE imports (from .core import ...), and a "
                "solution/main.py entrypoint guarded by `if __name__ == "
                "__main__:` that exercises the API. If the task implies "
                "behavior, write solution/test_main.py with pytest tests for "
                "the public API (they will be run by an independent verifier). "
                "Keep the code simple, dependency-free, and correct - an "
                "independent test node will verify it next.\n"
                f"Task: {task}\nPlan:\n{plan or '(none)'}"
            ),
            tools=[FunctionTool(write_file)],
        )
        node = ADKAgentNode(agent)
        return node(inputs, job_dir)

    return Node(
        id="build-multi", kind="tool", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="ADK LlmAgent scaffolds multi-file project (fails loud without a model)",
    )


def _build_verify_command() -> str:
    """Build the bash command for the verify node.

    Verifies the solution/ project:
      1. test_solution.py at root exists  -> run pytest (from solution import)
      2. solution/test_*.py exist         -> run pytest inside the package
      3. solution/main.py exists          -> run the entrypoint
      4. otherwise                        -> py_compile syntax check

    Always writes EVAL.json and exits 0 so the gate decides SHIP/FIX from
    the evidence, not the exit code.
    """
    return (
        "if [ -f test_solution.py ]; then "
        "  PYTHONPATH=. python3 -B -m pytest test_solution.py --tb=short -q > test_output.log 2>&1; rc=$?; "
        "elif ls solution/test_*.py >/dev/null 2>&1; then "
        "  PYTHONPATH=solution python3 -B -m pytest solution/test_*.py --tb=short -q > test_output.log 2>&1; rc=$?; "
        "elif [ -f solution/main.py ]; then "
        "  PYTHONPATH=. python3 -B solution/main.py > build.log 2>&1; rc=$?; "
        "else "
        "  python3 -B -m py_compile solution/*.py > build.log 2>&1; rc=$?; "
        "fi; "
        "if [ $rc -eq 0 ]; then "
        "  printf '{\"checks\":[{\"name\":\"multi-build-verified\",\"passed\":true,"
        "\"message\":\"solution builds and verifies\"}],\"exit_code\":0}' > EVAL.json; "
        "else "
        "  failed=$(grep -c 'FAILED' test_output.log 2>/dev/null) || true; "
        "  failed=${failed:-0}; "
        "  printf '{\"checks\":[{\"name\":\"multi-build-verified\",\"passed\":false,"
        "\"message\":\"exit %s, %s failed item(s)\"}],\"exit_code\":%s}'"
        " \"$rc\" \"$failed\" \"$rc\" > EVAL.json; "
        "fi; "
        "exit 0"
    )


def build_multi_hop() -> Hop:
    """The `build-multi` workflow: multi-file project scaffold + verify.

    Hop structure:
      1. build-multi (tool/ADK) - scaffolds solution/ project
      2. verify (bash)          - runs pytest/entrypoint, writes EVAL.json
    """
    wf = Workflow(id="build-multi", description="Scaffold a multi-file project")
    wf.add_node(_build_multi_adk_node())
    wf.add_node(Node(
        id="verify", kind="bash",
        command=_build_verify_command(),
        depends_on=["build-multi"],
        description="Verify solution/ project, write EVAL.json",
    ))
    return Hop(
        id="build-multi", workflow=wf,
        required_artifacts=["solution", "EVAL.json"],
        gate_checks={
            "eval-json": eval_json_check(),
            "exit-codes": exit_codes_check(),
            "artifacts": required_artifact_check(["solution", "EVAL.json"]),
        },
        max_fix_loops=2,
    )
