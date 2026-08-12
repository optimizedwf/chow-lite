"""Refactor workflow - restructure code, verify behavior intact.

The `refactor` lane of nine: given an existing solution.py (and optional
test_solution.py), an ADK LlmAgent planner writes REFACTOR_PLAN.md (an
edit-spec), a prompt node renders the before/after diff for human review
(DIFF.md), an ADK LlmAgent applies the restructure as refactored.py
(preserving the public interface), and an independent bash node re-runs
the original tests against the refactored code and writes EVAL.json +
REFACTOR_RECEIPT.json.

Model-or-fail: without GEMINI_API_KEY the model nodes raise WorkflowError -
the job fails loud. NEVER a canned refactor.
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
from nine.runtime.summarizer import _gemini_generate
from nine.runtime.workflows import Node, Workflow, WorkflowError


def _context_read_command() -> str:
    """Bash node: inventory the workspace + snapshot the original code.

    Writes CONTEXT.md (file inventory + baseline test status) and copies
    solution.py -> refactor_before.py so downstream nodes always have the
    original to compare against. Always exits 0; evidence is the artifact.
    """
    return (
        "echo '# Refactor Context' > CONTEXT.md; "
        "echo '## Files' >> CONTEXT.md; "
        "ls -la >> CONTEXT.md; "
        "if [ -f solution.py ]; then "
        "  cp solution.py refactor_before.py; "
        "  echo '## Source' >> CONTEXT.md; "
        "  echo 'solution.py copied to refactor_before.py' >> CONTEXT.md; "
        "else "
        "  echo '## Source: (no solution.py found)' >> CONTEXT.md; "
        "fi; "
        "if [ -f test_solution.py ]; then "
        "  python3 -B -m pytest test_solution.py --tb=short -q "
        "> baseline_test.log 2>&1; "
        "  echo 'baseline tests exit: $?' >> CONTEXT.md; "
        "else "
        "  echo 'baseline tests: none found' >> CONTEXT.md; "
        "fi; "
        "exit 0"
    )


def _planner_adk_node() -> Node:
    """ADK LlmAgent that writes REFACTOR_PLAN.md (edit-spec).

    Model-or-fail: raises WorkflowError without GEMINI_API_KEY.
    """
    def _run(inputs: dict, job_dir) -> dict:
        from nine.runtime.adk_runtime import ADKAgentNode

        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:500]
        if not os.environ.get("GEMINI_API_KEY"):
            raise WorkflowError(
                "refactor (planner) requires GEMINI_API_KEY (ADK LlmAgent) - "
                "no offline fallback, nine is model-driven"
            )

        from google.adk.agents import LlmAgent
        from google.adk.models import Gemini
        from google.adk.tools import FunctionTool

        def write_file(path: str, content: str) -> str:
            """Write a file into the refactor workspace (job dir)."""
            (job_dir / path).write_text(content, encoding="utf-8")
            return f"wrote {path} ({len(content)} bytes)"

        original = ""
        if (job_dir / "refactor_before.py").exists():
            original = (job_dir / "refactor_before.py").read_text(
                encoding="utf-8")[:4000]

        agent = LlmAgent(
            name="refactor-planner",
            model=Gemini(model="gemini-3.6-flash"),
            instruction=(
                "You are the refactor planner of nine, an evidence-gated "
                "agent OS. Design a code refactor and write REFACTOR_PLAN.md "
                "using the write_file tool. The plan must include:\n"
                "1. **Goal**: what is being restructured and why.\n"
                "2. **Behavior Contract**: the public interface (function "
                "names, signatures, observable behavior) that MUST NOT "
                "change.\n"
                "3. **Edit Spec**: exact before -> after changes (split "
                "functions, extract classes, rename internals, reorder, "
                "etc.) with line references.\n"
                "4. **Risks**: what could break and how the verify step "
                "would catch it.\n"
                "The refactor is behavior-preserving: no API or output "
                "semantics may change.\n"
                f"Task: {task}\n"
                f"Original code (refactor_before.py):\n"
                f"```python\n{original}\n```"
            ),
            tools=[FunctionTool(write_file)],
        )
        node = ADKAgentNode(agent)
        return node(inputs, job_dir)

    return Node(
        id="planner", kind="tool", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="ADK LlmAgent writes REFACTOR_PLAN.md (fails loud without a model)",
    )


def _diff_gate_node() -> Node:
    """Prompt node: render the before/after diff for human review (DIFF.md).

    This is the human-diff-gate: the model produces a concrete DIFF.md a
    human can read and approve before any code is touched. The diff must be
    explicit (snippets before -> after); empty model output fails loud.
    """
    def _run(inputs: dict, job_dir) -> dict:
        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:300]

        plan = ""
        if (job_dir / "REFACTOR_PLAN.md").exists():
            plan = (job_dir / "REFACTOR_PLAN.md").read_text(
                encoding="utf-8")[:4000]
        original = ""
        if (job_dir / "refactor_before.py").exists():
            original = (job_dir / "refactor_before.py").read_text(
                encoding="utf-8")[:3000]

        prompt = (
            "You are the human-diff-gate of nine, an evidence-gated agent "
            "OS. Render the proposed refactor as DIFF.md for a human "
            "reviewer: a '## Before' block and a '## After' block showing "
            "the key before -> after changes from the plan, plus one line "
            "per change explaining why it preserves behavior. Be concrete "
            "- show actual code snippets.\n"
            f"Task: {task}\n"
            f"REFACTOR_PLAN.md:\n{plan}\n"
            f"Original code:\n```python\n{original}\n```"
        )
        diff = _gemini_generate(prompt, api_key=None)
        if not (diff and diff.strip()):
            raise WorkflowError(
                "refactor diff-gate: model returned no diff - job failed "
                "loud (no offline fallback)"
            )
        (job_dir / "DIFF.md").write_text(diff.strip(), encoding="utf-8")
        return {"output": "wrote DIFF.md", "artifact_path": str(job_dir / "DIFF.md")}

    return Node(
        id="diff-gate", kind="prompt", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="Prompt node renders DIFF.md for human review (fails loud without a model)",
    )


def _apply_adk_node() -> Node:
    """ADK LlmAgent that applies the plan: writes refactored.py.

    Model-or-fail: raises WorkflowError without GEMINI_API_KEY.
    """
    def _run(inputs: dict, job_dir) -> dict:
        from nine.runtime.adk_runtime import ADKAgentNode

        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:500]
        if not os.environ.get("GEMINI_API_KEY"):
            raise WorkflowError(
                "refactor (apply) requires GEMINI_API_KEY (ADK LlmAgent) - "
                "no offline fallback, nine is model-driven"
            )

        from google.adk.agents import LlmAgent
        from google.adk.models import Gemini
        from google.adk.tools import FunctionTool

        def write_file(path: str, content: str) -> str:
            """Write a file into the refactor workspace (job dir)."""
            (job_dir / path).write_text(content, encoding="utf-8")
            return f"wrote {path} ({len(content)} bytes)"

        plan = ""
        if (job_dir / "REFACTOR_PLAN.md").exists():
            plan = (job_dir / "REFACTOR_PLAN.md").read_text(
                encoding="utf-8")[:4000]
        diff = ""
        if (job_dir / "DIFF.md").exists():
            diff = (job_dir / "DIFF.md").read_text(encoding="utf-8")[:3000]
        original = ""
        if (job_dir / "refactor_before.py").exists():
            original = (job_dir / "refactor_before.py").read_text(
                encoding="utf-8")[:4000]

        agent = LlmAgent(
            name="refactor-apply",
            model=Gemini(model="gemini-3.6-flash"),
            instruction=(
                "You are the refactor applier of nine, an evidence-gated "
                "agent OS. Apply the refactor plan to the original code and "
                "write the FULL refactored Python module as refactored.py "
                "using the write_file tool. Requirements:\n"
                "- Preserve the public interface exactly: function names, "
                "signatures, return semantics (the Behavior Contract).\n"
                "- The result must be a complete, runnable module (tests "
                "will import it via 'from refactored import ...').\n"
                "- If the original had a main/entrypoint, keep it working "
                "under if __name__ == '__main__'.\n"
                f"Task: {task}\n"
                f"REFACTOR_PLAN.md:\n{plan}\n"
                f"DIFF.md:\n{diff}\n"
                f"Original code:\n```python\n{original}\n```"
            ),
            tools=[FunctionTool(write_file)],
        )
        node = ADKAgentNode(agent)
        return node(inputs, job_dir)

    return Node(
        id="apply", kind="tool", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="ADK LlmAgent writes refactored.py (fails loud without a model)",
    )


def _verify_command() -> str:
    """Bash node: run the original tests against refactored.py.

    If test_solution.py exists, substitute solution imports -> refactored
    and run pytest; otherwise run refactored.py directly. Always writes
    EVAL.json + REFACTOR_RECEIPT.json and exits 0 so the gate decides
    SHIP/FIX from the evidence (never from an exit code).
    """
    return r"""
if [ -f test_solution.py ]; then
  sed 's/from solution import/from refactored import/g; s/import solution/import refactored/g' test_solution.py > test_refactored.py
  python3 -B -m pytest test_refactored.py --tb=short -q > refactor_test.log 2>&1
  rc=$?
  if [ $rc -eq 0 ]; then
    printf '{"checks":[{"name":"refactor-verified","passed":true,"message":"all tests pass with refactored.py"}],"exit_code":0}' > EVAL.json
  else
    failed=$(grep -c 'FAILED' refactor_test.log 2>/dev/null || true)
    failed=${failed:-0}
    printf '{"checks":[{"name":"refactor-verified","passed":false,"message":"%s test(s) failed"}],"exit_code":%s}' "$failed" "$rc" > EVAL.json
  fi
else
  python3 -B refactored.py > refactor_run.log 2>&1
  rc=$?
  if [ $rc -eq 0 ]; then
    printf '{"checks":[{"name":"refactor-runs","passed":true,"message":"refactored.py exit 0"}],"exit_code":0}' > EVAL.json
  else
    printf '{"checks":[{"name":"refactor-runs","passed":false,"message":"exit %s"}],"exit_code":%s}' "$rc" "$rc" > EVAL.json
  fi
fi
passed=$(python3 -c 'import json,sys; print(str(json.load(open("EVAL.json"))["checks"][0]["passed"]).lower())')
printf '{"refactor":"refactor_before.py -> refactored.py","plan":"REFACTOR_PLAN.md","diff":"DIFF.md","tests_passed":%s,"exit_code":%s}' "$passed" "$rc" > REFACTOR_RECEIPT.json
exit 0
"""


def refactor_hop() -> Hop:
    """The `refactor` workflow: restructure code, verify behavior intact.

    Five-node hop:
      1. context-read (bash)   - inventory + snapshot original (CONTEXT.md,
                                 refactor_before.py, baseline_test.log)
      2. planner (tool/ADK)    - writes REFACTOR_PLAN.md (edit-spec)
      3. diff-gate (prompt)    - renders DIFF.md (before/after) for human review
      4. apply (tool/ADK)      - writes refactored.py (behavior-preserving)
      5. verify (bash)         - re-runs original tests vs refactored.py,
                                 writes EVAL.json + REFACTOR_RECEIPT.json

    Note: __test__ = False is set below to prevent pytest from collecting
    this as a test function (name starts with test_).
    """
    wf = Workflow(id="refactor", description="Restructure code and verify behavior intact")
    context = Node(id="context-read", kind="bash", command=_context_read_command(),
                   description="Inventory workspace + snapshot original code")
    planner = _planner_adk_node()
    planner.depends_on = ["context-read"]
    diff = _diff_gate_node()
    diff.depends_on = ["planner"]
    apply = _apply_adk_node()
    apply.depends_on = ["planner", "diff-gate"]
    verify = Node(id="verify", kind="bash", command=_verify_command(),
                  depends_on=["apply"],
                  description="Run original tests against refactored.py, write EVAL.json + REFACTOR_RECEIPT.json")
    for n in (context, planner, diff, apply, verify):
        wf.add_node(n)
    return Hop(
        id="refactor", workflow=wf,
        required_artifacts=["REFACTOR_PLAN.md", "DIFF.md", "refactored.py",
                            "EVAL.json", "REFACTOR_RECEIPT.json"],
        gate_checks={
            "eval-json": eval_json_check(),
            "exit-codes": exit_codes_check(),
            "artifacts": required_artifact_check(
                ["REFACTOR_PLAN.md", "DIFF.md", "refactored.py",
                 "EVAL.json", "REFACTOR_RECEIPT.json"]
            ),
        },
        max_fix_loops=2,
    )


refactor_hop.__test__ = False  # type: ignore[attr-defined]  # prevent pytest collection
