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

from pathlib import Path

from nine.chains.chain import Hop
from nine.gates.evidence import (
    eval_json_check,
    exit_codes_check,
    required_artifact_check,
)
from nine.runtime.fsafety import contained_write
from nine.runtime.llm_provider import key_available
from nine.runtime.workflows import Node, Workflow


def _env_cap(name: str, default: int = 1400) -> int:
    """Read an int env knob with the junk-env convention (torture-25 F3):
    a non-numeric value (e.g. NINE_TASK_CAP=2k) prints ONE loud stderr
    warning naming the variable and falls back to `default` — mirroring
    NINE_MAX_LLM_CALLS (T24-F5) / NINE_GATE_TIMEOUT_S (T22-F2) behavior.
    A value < 1 is treated the same as junk (a 0-char task cap would
    silently empty every task)."""
    import os as _os
    raw = _os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(f"WARNING: {name}={raw!r} is not an integer - using {default}",
              file=_os.sys.stderr if hasattr(_os, "sys") else __import__("sys").stderr)
        return default
    if value < 1:
        print(f"WARNING: {name}={raw!r} is < 1 - using {default}",
              file=_os.sys.stderr if hasattr(_os, "sys") else __import__("sys").stderr)
        return default
    return value


def _cap_instruction(instruction: str, limit: int = 0) -> str:
    import os as _os
    if not limit:
        limit = _env_cap("NINE_INSTRUCTION_LIMIT")
    if _os.environ.get("NINE_DEBUG_INSTR"):
        print(f"[cap] len={len(instruction)} limit={limit} capped={len(instruction)>limit}", flush=True)
    """slice-40: qwen3:8b tool-calling degenerates when the system prompt
    grows long — with ~1000+ chars of appended context it burns its ENTIRE
    max_tokens budget (finish:"length", no tool call, no text). Cap the
    built instruction at `limit` chars, keeping the FRONT (role + task)
    and the tail (code context), with an ellipsis marker in between.

    slice-45: the original 700-char cap dropped the critical STRICT-REJECT
    requirements in the middle of fixture tasks (e.g. bugfix-small-006's
    validate list), so the diagnose agent misread the bug. Default raised
    to 1400 (env NINE_INSTRUCTION_LIMIT) — qwen3:8b handles ~1400 chars of
    instruction fine with the /api/chat no-thinking fix; only re-lower if
    degeneration (finish:length, no tool call) reappears.
    """
    if len(instruction) <= limit:
        return instruction
    head = instruction[: int(limit * 0.6)]
    tail = instruction[-(limit - int(limit * 0.6)) :]
    return head + "\n...[context truncated for model budget]...\n" + tail



def _diagnose_adk_node() -> Node:
    """ADK LlmAgent that reads the symptom + existing code and writes ROOT_CAUSE.md.

    Model-or-fail: raises WorkflowError without GEMINI_API_KEY.
    """
    from nine.runtime.workflows import WorkflowError

    def _run(inputs: dict, job_dir) -> dict:
        from nine.runtime.adk_runtime import ADKAgentNode

        job_dir = Path(job_dir)
        # slice-45: task cap raised 700->1400 (env NINE_TASK_CAP) — the
        # 700 cap dropped the STRICT-REJECT requirements in the middle of
        # fixture tasks (006's validate list), so the diagnose agent
        # misread the bug. qwen3:8b handles 1400 chars with the /api/chat
        # no-thinking fix.
        _task_cap = _env_cap("NINE_TASK_CAP")
        task = str(inputs.get("task", ""))[:_task_cap]
        fix_dir = str(inputs.get("fix_directive", ""))[:1500]
        if not key_available():
            raise WorkflowError(
                "debug requires an LLM key (gemini: GEMINI_API_KEY; openai: NINE_LLM_API_KEY/OPENCODE_GO_API_KEY) (ADK LlmAgent) - no offline "
                "fallback, nine is model-driven"
            )

        from google.adk.agents import LlmAgent
        from google.adk.tools import FunctionTool

        from nine.runtime import llm_provider

        def write_file(path: str, content: str) -> str:
            """Write a file into the debug workspace (job dir)."""
            contained_write(job_dir, path, content)
            return (f"wrote {path} ({len(content)} bytes) — FILE WRITE "
                    "COMPLETE. Do NOT rewrite this file; the hop is DONE: "
                    "reply with a one-line summary.")

        # Gather context: existing code, error logs, test output
        solution = ""
        if (job_dir / "solution.py").exists():
            solution = (job_dir / "solution.py").read_text(encoding="utf-8")[:700]

        test_out = ""
        if (job_dir / "test_output.log").exists():
            test_out = (job_dir / "test_output.log").read_text(encoding="utf-8")[:2000]

        build_log = ""
        if (job_dir / "build.log").exists():
            build_log = (job_dir / "build.log").read_text(encoding="utf-8")[:1000]

        test_code = ""
        if (job_dir / "test_solution.py").exists():
            test_code = (job_dir / "test_solution.py").read_text(encoding="utf-8")[:500]

        instruction = (
            "You are the diagnose hop of nine, an evidence-gated agent OS.\n"
            "Read the symptom/task and all available code + logs below.\n"
            "Diagnose the root cause of the failure and write ROOT_CAUSE.md\n"
            "using the write_file tool. The document must include:\n"
            "1. **Symptom**: what is failing (errors, test failures, etc.)\n"
            "2. **Root Cause**: the specific line(s) or logic causing the issue\n"
            "3. **Fix Plan**: a step-by-step plan to patch the code\n"
            "4. **Risk**: what else might break after the fix\n"
            "IMPORTANT: the supplied code is assumed to be RUNNABLE Python \n"
            "(no syntax errors) unless a test output says otherwise — look \n"
            "for LOGIC bugs (wrong values, wrong types, wrong conditions), \n"
            "not imaginary syntax errors.\n\n"
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
            model=llm_provider.adk_model(),
            instruction=_cap_instruction(instruction),
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
        # slice-45: task cap raised 700->1400 (see diagnose node).
        _task_cap = _env_cap("NINE_TASK_CAP")
        task = str(inputs.get("task", ""))[:_task_cap]
        fix_dir = str(inputs.get("fix_directive", ""))[:1500]
        if not key_available():
            raise WorkflowError(
                "debug requires an LLM key (gemini: GEMINI_API_KEY; openai: NINE_LLM_API_KEY/OPENCODE_GO_API_KEY) (ADK LlmAgent) - no offline "
                "fallback, nine is model-driven"
            )

        from google.adk.agents import LlmAgent
        from google.adk.tools import FunctionTool

        from nine.runtime import llm_provider

        def write_file(path: str, content: str) -> str:
            """Write a file into the debug workspace (job dir)."""
            contained_write(job_dir, path, content)
            return (f"wrote {path} ({len(content)} bytes) — FILE WRITE "
                    "COMPLETE. Do NOT rewrite this file; the hop is DONE: "
                    "reply with a one-line summary.")

        root_cause = ""
        if (job_dir / "ROOT_CAUSE.md").exists():
            root_cause = (job_dir / "ROOT_CAUSE.md").read_text(encoding="utf-8")[:3000]

        solution = ""
        if (job_dir / "solution.py").exists():
            solution = (job_dir / "solution.py").read_text(encoding="utf-8")[:700]

        test_code = ""
        if (job_dir / "test_solution.py").exists():
            test_code = (job_dir / "test_solution.py").read_text(encoding="utf-8")[:500]

        seeded_test = (job_dir / "test_solution.py").exists()
        instruction = (
            "You are the patch hop of nine. Fix the bug and write `patch.py` "
            "(write_file tool) with the EXACT SAME function names and "
            "signatures as the original code — do NOT rename functions and "
            "do NOT invent new ones. "
            + (
                "Also write `test_solution.py` — a SECOND write_file call — "
                "pytest importing from patch (`from patch import ...`); "
                "verify will run it. NO patch without tests passes. Write "
                "PLAIN pytest functions `def test_xxx():` with direct asserts "
                "(do NOT use pytest.raises inside parametrize loops or table "
                "drivers — one function per case, `with pytest.raises(...)` "
                "inline). "
                if not seeded_test
                else "test_solution.py ALREADY EXISTS — do NOT overwrite "
                "it; write ONLY patch.py. "
            )
            + "CRITICAL: if the code builds JSON, use `json.dumps({...})` for "
            "the ENTIRE object — NEVER hand-write JSON with string "
            "concatenation (no `'{\\n...' + ...` patterns, no %-formatting "
            "of JSON). Hand-built JSON strings break (stray brace / "
            "unterminated string / mismatched quote). Serialize booleans "
            "with json.dumps so Python True becomes JSON true. "
            "\n\n"
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
            model=llm_provider.adk_model(),
            instruction=_cap_instruction(instruction),
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
        "  if [ -f patch.py ] && ! grep -qE 'import patch|from patch' test_patch.py; then "
        "    printf 'from patch import *\\n' | cat - test_patch.py > test_patch_tmp && mv test_patch_tmp test_patch.py; "
        "  fi; "
        "  python3 -B -m pytest test_patch.py --tb=short -q > test_output.log 2>&1; "
        "  rc=$?; "
        "  if [ $rc -eq 5 ] || grep -qE 'no tests ran|ERROR collecting' test_output.log; then "
        "  printf '{\"checks\":[{\"name\":\"patch-verified\",\"passed\":false,"
        "\"message\":\"pytest collection error\"}],\"exit_code\":1}' > EVAL.json; "
        "  exit 0; "
        "  fi; "
        "  if [ $rc -eq 0 ]; then "
        "  printf '{\"checks\":[{\"name\":\"patch-verified\",\"passed\":true,"
        "\"message\":\"all tests pass with patch\"}],\"exit_code\":0}' > EVAL.json; "
        "  else "
        "  failed=$(grep -oE '[0-9]+ failed' test_output.log | tail -1 | grep -oE '[0-9]+' || true); "
        "  passed=$(grep -oE '[0-9]+ passed' test_output.log | tail -1 | grep -oE '[0-9]+' || true); "
        "  failed=${failed:-0}; passed=${passed:-0}; "
        "  printf '{\"checks\":[{\"name\":\"patch-verified\",\"passed\":false,"
        "\"message\":\"%s test(s) failed, %s passed\"}],\"exit_code\":%s}'"
        " \"$failed\" \"$passed\" \"$rc\" > EVAL.json; "
        "  exit 0; "
        "  fi; "
        "else "
        "  if [ -f test_patch.py ] || ls test_*.py >/dev/null 2>&1; then "
        "    python3 -B -m pytest test_*.py --tb=short -q > test_output.log 2>&1; rc=$?; "
        "    if [ $rc -eq 0 ]; then "
        "    printf '{\"checks\":[{\"name\":\"patch-verified\",\"passed\":true,"
        "\"message\":\"all tests pass with patch\"}],\"exit_code\":0}' > EVAL.json; "
        "    else "
        "    printf '{\"checks\":[{\"name\":\"patch-verified\",\"passed\":false,"
        "\"message\":\"test(s) failed with patch\"}],\"exit_code\":%s}' \"$rc\" > EVAL.json; "
        "    fi; "
        "    exit 0; "
        "  fi; "
        "  python3 -B patch.py > build.log 2>&1; rc=$?; "
        "  printf '{\"checks\":[{\"name\":\"patch-runs\",\"passed\":false,"
        "\"message\":\"no test evidence - patch runs but unverified (write test_solution.py)\"}],"
        "\"exit_code\":1}' > EVAL.json; "
        "  exit 0; "
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
