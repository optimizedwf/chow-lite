"""Test workflow — write and run pytest tests for a task or solution.

This is the `test` lane of nine: given a task (and optionally a solution.py
already produced by the build hop), an ADK LlmAgent writes real pytest
tests, then an independent bash node runs pytest and writes EVAL.json.

Model-or-fail: without GEMINI_API_KEY the ADK node raises WorkflowError —
the job fails loud. NEVER a canned test file.
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
from nine.runtime.fsafety import contained_write
from nine.runtime.workflows import Node, Workflow


def _test_adk_node() -> Node:
    """ADK LlmAgent that writes test_solution.py with real pytest assertions.

    If solution.py exists in the job dir, tests are white-box (testing the
    actual implementation). Otherwise tests are black-box (generated from
    the task specification alone), assuming a solution.py will exist at run
    time.

    Model-or-fail: raises WorkflowError without GEMINI_API_KEY.
    """
    from nine.runtime.workflows import WorkflowError

    def _run(inputs: dict, job_dir) -> dict:
        from nine.runtime.adk_runtime import ADKAgentNode

        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:1500]
        if not os.environ.get("GEMINI_API_KEY", "").strip():
            raise WorkflowError(
                "test requires GEMINI_API_KEY (ADK LlmAgent) — no offline "
                "fallback, nine is model-driven"
            )

        from google.adk.agents import LlmAgent
        from google.adk.models import Gemini
        from google.adk.tools import FunctionTool

        def write_file(path: str, content: str) -> str:
            """Write a source file into the test workspace (job dir)."""
            contained_write(job_dir, path, content)
            return f"wrote {path} ({len(content)} bytes)"

        solution = ""
        if (job_dir / "solution.py").exists():
            solution = (job_dir / "solution.py").read_text(encoding="utf-8")[:2000]

        plan = ""
        if (job_dir / "PLAN.md").exists():
            plan = (job_dir / "PLAN.md").read_text(encoding="utf-8")[:800]

        if solution:
            instruction = (
                "You are the test hop of nine, an evidence-gated agent OS.\n"
                "Read the task and the solution.py below, then write a pytest "
                "module `test_solution.py` that imports and tests the solution. "
                "Use the write_file tool. Write REAL assertions that verify "
                "correct behavior — at least 3 test functions covering normal "
                "cases, edge cases, and error handling. Do NOT import modules "
                "that are not installed. Keep tests self-contained.\n"
                f"Task: {task}\n"
                f"solution.py:\n```python\n{solution}\n```\n"
                f"Plan:\n{plan or '(none)'}"
            )
        else:
            instruction = (
                "You are the test hop of nine, an evidence-gated agent OS.\n"
                "Read the task below and write a pytest module `test_solution.py` "
                "with black-box tests derived from the specification. Use the "
                "write_file tool. Write REAL assertions — at least 3 test "
                "functions covering expected behavior, edge cases, and error "
                "handling. Assume a module `solution.py` will exist alongside "
                "the tests at run time. Do NOT import modules that are not "
                "installed.\n"
                f"Task: {task}\n"
                f"Plan:\n{plan or '(none)'}"
            )

        agent = LlmAgent(
            name="tester",
            model=Gemini(model="gemini-3.6-flash"),
            instruction=instruction,
            tools=[FunctionTool(write_file)],
        )
        node = ADKAgentNode(agent)
        return node(inputs, job_dir)

    return Node(
        id="test-writer", kind="tool", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="ADK LlmAgent writes test_solution.py (fails loud without a model)",
    )


def _build_test_runner_command() -> str:
    """Build the bash command for the test-runner node.

    Runs pytest on test_solution.py, captures results, and writes EVAL.json
    with pass/fail status. Non-zero exit on test failure is expected — the
    gate reads EVAL.json to decide SHIP/FIX, not the exit code alone.
    """
    # The test-runner must report test results as EVAL.json, not crash.
    # pytest exits non-zero on test failures, so we always write EVAL.json
    # and exit 0 so the gate can evaluate.
    return (
        "python3 -B -m pytest test_solution.py --tb=short -q > test_output.log 2>&1; "
        'rc=$?; '
        "if grep -qE 'error|no tests ran|collection' test_output.log; then "
        '  echo \'{"checks":[{"name":"tests-pass","passed":false,"message":"pytest collection error"}],"exit_code":1}\' > EVAL.json; '
        "elif [ $rc -eq 0 ]; then "
        '  echo \'{"checks":[{"name":"tests-pass","passed":true,"message":"all tests passed"}],"exit_code":0}\' > EVAL.json; '
        "else "
        "  failed=$(grep -c 'FAILED' test_output.log 2>/dev/null) || true; "
        "  failed=${failed:-0}; "
        "  passed=$(grep -c ' PASSED' test_output.log 2>/dev/null) || true; "
        "  passed=${passed:-0}; "
        "  printf '{\"checks\":[{\"name\":\"tests-pass\",\"passed\":false,"
        "\"message\":\"%s test(s) failed, %s passed\"}],\"exit_code\":%s}'"
        " \"$failed\" \"$passed\" \"$rc\" > EVAL.json; "
        "  exit 0; "
        "fi"
    )


def test_hop() -> Hop:
    """The `test` workflow: write + run pytest tests.

    Hop structure:
      1. test-writer (tool/ADK) - writes test_solution.py
      2. test-runner (bash)   - runs pytest, writes EVAL.json

    Note: __test__ = False is set below to prevent pytest from collecting
    this as a test function (name starts with test_).
    """
    wf = Workflow(id="test", description="Write and run pytest tests")
    wf.add_node(_test_adk_node())
    wf.add_node(Node(
        id="test-runner", kind="bash",
        command=_build_test_runner_command(),
        depends_on=["test-writer"],
        description="Run pytest on test_solution.py, write EVAL.json",
    ))
    return Hop(
        id="test", workflow=wf,
        required_artifacts=["test_solution.py", "EVAL.json"],
        gate_checks={
            "eval-json": eval_json_check(),
            "exit-codes": exit_codes_check(),
            "artifacts": required_artifact_check(["test_solution.py", "EVAL.json"]),
        },
        max_fix_loops=2,
    )


test_hop.__test__ = False  # type: ignore[attr-defined]  # prevent pytest collection
