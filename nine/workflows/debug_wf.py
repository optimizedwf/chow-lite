"""Debug workflow - root-cause a failure, patch it, verify the fix.

This is the `debug` lane of nine: given a symptom (error description, failing
test output, or a task + broken solution), an ADK LlmAgent diagnoses the root
cause and writes ROOT_CAUSE.md, a second ADK agent applies the fix as patch.py
(a corrected version of the code), and an independent bash node runs pytest
or the solution to verify the fix and writes EVAL.json.

Model-or-fail: without GEMINI_API_KEY the ADK nodes raise WorkflowError -
the job fails loud. NEVER a canned patch.
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


def _diagnose_adk_node() -> Node:
    """ADK LlmAgent that reads the symptom + existing code and writes ROOT_CAUSE.md.

    Model-or-fail: raises WorkflowError without GEMINI_API_KEY.
    """
    from nine.runtime.workflows import WorkflowError

    def _run(inputs: dict, job_dir) -> dict:
        from nine.runtime.adk_runtime import ADKAgentNode

        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:1500]
        fix_dir = str(inputs.get("fix_directive", ""))[:1500]
        if not os.environ.get("GEMINI_API_KEY"):
            raise WorkflowError(
                "debug requires GEMINI_API_KEY (ADK LlmAgent) - no offline "
                "fallback, nine is model-driven"
            )

        from google.adk.agents import LlmAgent
        from google.adk.models import Gemini
        from google.adk.tools import FunctionTool

        def write_file(path: str, content: str) -> str:
            """Write a file into the debug workspace (job dir)."""
            (job_dir / path).write_text(content, encoding="utf-8")
            return f"wrote {path} ({len(content)} bytes)"

        # Gather context: existing code, error logs, test output
        solution = ""
        if (job_dir / "solution.py").exists():
            solution = (job_dir / "solution.py").read_text(encoding="utf-8")[:3000]

        test_out = ""
        if (job_dir / "test_output.log").exists():
            test_out = (job_dir / "test_output.log").read_text(encoding="utf-8")[:2000]

        build_log = ""
        if (job_dir / "build.log").exists():
            build_log = (job_dir / "build.log").read_text(encoding="utf-8")[:1000]

        test_code = ""
        if (job_dir / "test_solution.py").exists():
            test_code = (job_dir / "test_solution.py").read_text(encoding="utf-8")[:1500]

        instruction = (
            "You are the diagnose hop of nine, an evidence-gated agent OS.\n"
            "Read the symptom/task and all available code + logs below.\n"
            "Diagnose the root cause of the failure and write ROOT_CAUSE.md\n"
            "using the write_file tool. The document must include:\n"
            "1. **Symptom**: what is failing (errors, test failures, etc.)\n"
            "2. **Root Cause**: the specific line(s) or logic causing the issue\n"
            "3. **Fix Plan**: a step-by-step plan to patch the code\n"
            "4. **Risk**: what else might break after the fix\n\n"
            f"Task/symptom: {task}\n"
        )
        if fix_dir:
            instruction += f"\nPrevious fix didn't pass: {fix_dir}\n"
        if solution:
            instruction += f"\nsolution.py:\n```python\n{solution}\n```\n"
        if test_code:
            instruction += f"\ntest_solution.py:\n```python\n{test_code}\n```\n"
        if test_out:
            instruction += f"\ntest output:\n```\n{test_out}\n```\n"
        if build_log:
            instruction += f"\nbuild log:\n```\n{build_log}\n```\n"

        agent = LlmAgent(
            name="diagnostician",
            model=Gemini(model="gemini-3.6-flash"),
            instruction=instruction,
            tools=[FunctionTool(write_file)],
        )
        node = ADKAgentNode(agent)
        return node(inputs, job_dir)

    return Node(
        id="diagnose", kind="tool", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="ADK LlmAgent writes ROOT_CAUSE.md (fails loud without a model)",
    )


def _patch_adk_node() -> Node:
    """ADK LlmAgent that reads ROOT_CAUSE.md and writes patch.py (fixed code).

    Model-or-fail: raises WorkflowError without GEMINI_API_KEY.
    """
    from nine.runtime.workflows import WorkflowError

    def _run(inputs: dict, job_dir) -> dict:
        from nine.runtime.adk_runtime import ADKAgentNode

        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:1500]
        fix_dir = str(inputs.get("fix_directive", ""))[:1500]
        if not os.environ.get("GEMINI_API_KEY"):
            raise WorkflowError(
                "debug requires GEMINI_API_KEY (ADK LlmAgent) - no offline "
                "fallback, nine is model-driven"
            )

        from google.adk.agents import LlmAgent
        from google.adk.models import Gemini
        from google.adk.tools import FunctionTool

        def write_file(path: str, content: str) -> str:
            """Write a file into the debug workspace (job dir)."""
            (job_dir / path).write_text(content, encoding="utf-8")
            return f"wrote {path} ({len(content)} bytes)"

        root_cause = ""
        if (job_dir / "ROOT_CAUSE.md").exists():
            root_cause = (job_dir / "ROOT_CAUSE.md").read_text(encoding="utf-8")[:3000]

        solution = ""
        if (job_dir / "solution.py").exists():
            solution = (job_dir / "solution.py").read_text(encoding="utf-8")[:3000]

        test_code = ""
        if (job_dir / "test_solution.py").exists():
            test_code = (job_dir / "test_solution.py").read_text(encoding="utf-8")[:1500]

        instruction = (
            "You are the patch hop of nine, an evidence-gated agent OS.\n"
            "Read the root cause analysis and the original code below.\n"
            "Write a corrected Python module `patch.py` that fixes the issue.\n"
            "Use the write_file tool. The patch must be a complete, runnable\n"
            "Python module that can be tested independently. If the original\n"
            "code had a function `solution.py`, replicate its public interface\n"
            "(function names, signatures) in patch.py so tests can import it.\n\n"
        )
        if root_cause:
            instruction += f"ROOT_CAUSE.md:\n{root_cause}\n\n"
        else:
            instruction += f"Task/symptom: {task}\n\n"
        if fix_dir:
            instruction += f"Previous fix didn't pass: {fix_dir}\n\n"
        if solution:
            instruction += f"Original solution.py:\n```python\n{solution}\n```\n\n"
        if test_code:
            instruction += f"test_solution.py:\n```python\n{test_code}\n```\n\n"

        agent = LlmAgent(
            name="patcher",
            model=Gemini(model="gemini-3.6-flash"),
            instruction=instruction,
            tools=[FunctionTool(write_file)],
        )
        node = ADKAgentNode(agent)
        return node(inputs, job_dir)

    return Node(
        id="patch", kind="tool", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="ADK LlmAgent writes patch.py (fails loud without a model)",
    )


def _build_verify_command() -> str:
    """Build the bash command for the verify node.

    Runs pytest if test_solution.py exists (importing from patch.py instead
    of solution.py), otherwise runs patch.py directly. Always writes EVAL.json
    and exits 0 so the gate can decide SHIP/FIX from the evidence.
    """
    return (
        "if [ -f test_solution.py ]; then "
        "  sed 's/from solution import/from patch import/g; "
        "s/import solution/import patch/g' test_solution.py > test_patch.py; "
        "  python3 -B -m pytest test_patch.py --tb=short -q > test_output.log 2>&1; "
        "  rc=$?; "
        "  if grep -qE 'error|no tests ran|collection' test_output.log; then "
        "  printf '{\"checks\":[{\"name\":\"patch-verified\",\"passed\":false,"
        "\"message\":\"pytest collection error\"}],\"exit_code\":1}' > EVAL.json; "
        "  exit 0; "
        "  fi; "
        "  if [ $rc -eq 0 ]; then "
        "  printf '{\"checks\":[{\"name\":\"patch-verified\",\"passed\":true,"
        "\"message\":\"all tests pass with patch\"}],\"exit_code\":0}' > EVAL.json; "
        "  else "
        "  failed=$(grep -c 'FAILED' test_output.log 2>/dev/null) || true; "
        "  passed=$(grep -c ' PASSED' test_output.log 2>/dev/null) || true; "
        "  failed=${failed:-0}; passed=${passed:-0}; "
        "  printf '{\"checks\":[{\"name\":\"patch-verified\",\"passed\":false,"
        "\"message\":\"%s test(s) failed, %s passed\"}],\"exit_code\":%s}'"
        " \"$failed\" \"$passed\" \"$rc\" > EVAL.json; "
        "  exit 0; "
        "  fi; "
        "else "
        "  python3 -B patch.py > build.log 2>&1; rc=$?; "
        "  if [ $rc -eq 0 ]; then "
        "  printf '{\"checks\":[{\"name\":\"patch-runs\",\"passed\":true,"
        "\"message\":\"patch.py exit 0\"}],\"exit_code\":0}' > EVAL.json; "
        "  else "
        "  printf '{\"checks\":[{\"name\":\"patch-runs\",\"passed\":false,"
        "\"message\":\"exit %s\"}],\"exit_code\":%s}' \"$rc\" \"$rc\" > EVAL.json; "
        "  exit 0; "
        "  fi; "
        "fi"
    )


def debug_hop() -> Hop:
    """The `debug` workflow: root-cause a failure, patch it, verify the fix.

    Three-node hop:
      1. diagnose (tool/ADK) - reads symptom + code/logs, writes ROOT_CAUSE.md
      2. patch (tool/ADK)    - reads ROOT_CAUSE.md + original code, writes patch.py
      3. verify (bash)       - runs pytest (or patch.py), writes EVAL.json

    Note: __test__ = False is set below to prevent pytest from collecting
    this as a test function (name starts with test_).
    """
    wf = Workflow(id="debug", description="Root-cause failure, patch, and verify")
    wf.add_node(_diagnose_adk_node())
    wf.add_node(_patch_adk_node())
    wf.add_node(Node(
        id="verify", kind="bash",
        command=_build_verify_command(),
        depends_on=["diagnose", "patch"],
        description="Run pytest with patch.py substituted, write EVAL.json",
    ))
    return Hop(
        id="debug", workflow=wf,
        # ROOT_CAUSE.md is advisory, NOT gate evidence: a perfect patch must
        # SHIP even when the diagnose agent never wrote the diagnosis doc.
        # The verify node's EVAL.json is the only fix evidence that matters.
        required_artifacts=["patch.py", "EVAL.json"],
        gate_checks={
            "eval-json": eval_json_check(),
            "exit-codes": exit_codes_check(),
            "artifacts": required_artifact_check(
                ["patch.py", "EVAL.json"]
            ),
        },
        max_fix_loops=2,
    )


debug_hop.__test__ = False  # type: ignore[attr-defined]  # prevent pytest collection
