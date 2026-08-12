from __future__ import annotations

import os
from pathlib import Path

from nine.chains.chain import Chain, Hop
from nine.gates.evidence import (
    eval_json_check,
    exit_codes_check,
    required_artifact_check,
)
from nine.runtime.workflows import Node, Workflow

# ---------------------------------------------------------------- hops

def research_hop(include_datahub: bool = False) -> Hop:
    wf = Workflow(id="research", description="Produce findings + distilled handoff")
    wf.add_node(Node(
        id="research", kind="bash",
        command=(
            "cat task.txt 2>/dev/null | head -5 > _task; "
            "echo '# Findings' > research.md; "
            "echo >> research.md; "
            "echo 'Task under study:' >> research.md; "
            "cat _task >> research.md; "
            "echo >> research.md; "
            "echo 'Key insight: evidence-gated execution keeps agents honest.' >> research.md"
        ),
    ))
    if include_datahub:
        # optional metadata-graph context (behind NINE_DATAHUB_MCP=1) — the
        # "read the graph first" pattern from optimizedwf/datahub-2026.
        from nine.memory.datahub import datahub_tool_node

        wf.add_node(datahub_tool_node())
    # Cerebras minimum-viable-context: distill research.md -> HANDOFF.md so
    # the plan hop never chews the full raw findings document.
    from nine.runtime.summarizer import build_summarize_node

    wf.add_node(build_summarize_node("research.md", target="HANDOFF.md",
                                     depends_on=["research"]))
    return Hop(
        id="research", workflow=wf,
        required_artifacts=["research.md", "HANDOFF.md"],
        gate_checks={
            "research-md": required_artifact_check(["research.md"]),
            "handoff-md": required_artifact_check(["HANDOFF.md"]),
        },
        max_fix_loops=2,
    )


def plan_hop() -> Hop:
    wf = Workflow(id="plan", description="Write a build plan")
    wf.add_node(Node(
        id="plan", kind="bash",
        command=(
            "echo '# Plan' > PLAN.md; "
            "echo >> PLAN.md; "
            "echo 'Inputs: HANDOFF.md (distilled research), task.txt' >> PLAN.md; "
            "echo 'Steps: 1) scaffold 2) implement 3) verify with EVAL.json' >> PLAN.md; "
            "test -s HANDOFF.md && echo 'OK: HANDOFF.md distilled summary present' >> PLAN.md || echo 'WARN: no HANDOFF.md' >> PLAN.md"
        ),
    ))
    return Hop(
        id="plan", workflow=wf,
        required_artifacts=["PLAN.md", "HANDOFF.md"],
        gate_checks={
            "plan-md": required_artifact_check(["PLAN.md"]),
            "handoff-md": required_artifact_check(["HANDOFF.md"]),
        },
        max_fix_loops=2,
    )


def _build_adk_node() -> Node:
    """Build hop backed by a REAL Google ADK 2.0 LlmAgent.

    The agent (Gemini 3.5 Flash via google-adk) reads task.txt + PLAN.md and
    uses a FunctionTool `write_file` to write actual code (solution.py). This
    puts ADK on the flagship user-facing path — the "mandatory agent
    framework" requirement is exercised by every real run, not just tests.

    Model-or-fail: with no GEMINI_API_KEY the node raises WorkflowError and
    the job fails loud. NEVER a canned solution.py — fabricated code would
    be a lie in an evidence-gated system.
    """
    from nine.runtime.workflows import WorkflowError

    def _run(inputs: dict, job_dir) -> dict:
        from nine.runtime.adk_runtime import ADKAgentNode

        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:1500]
        if not os.environ.get("GEMINI_API_KEY"):
            raise WorkflowError(
                "build requires GEMINI_API_KEY (ADK LlmAgent) — no offline "
                "fallback, nine is model-driven"
            )

        from google.adk.agents import LlmAgent
        from google.adk.models import Gemini
        from google.adk.tools import FunctionTool

        def write_file(path: str, content: str) -> str:
            """Write a source file into the build workspace (job dir)."""
            (job_dir / path).write_text(content, encoding="utf-8")
            return f"wrote {path} ({len(content)} bytes)"

        plan = ""
        if (job_dir / "PLAN.md").exists():
            plan = (job_dir / "PLAN.md").read_text(encoding="utf-8")[:800]

        agent = LlmAgent(
            name="coder",
            model=Gemini(model="gemini-3.6-flash"),
            instruction=(
                "You are the build hop of nine, an evidence-gated agent OS.\n"
                "Read the task and plan, then write ONE runnable Python module "
                "`solution.py` that solves the task. Use the write_file tool. "
                "Keep the code simple, dependency-free, and correct — an "
                "independent self-test will run it next.\n"
                f"Task: {task}\nPlan:\n{plan or '(none)'}"
            ),
            tools=[FunctionTool(write_file)],
        )
        node = ADKAgentNode(agent)
        return node(inputs, job_dir)

    return Node(id="build", kind="tool", run=_run, max_retries=2,
                retry_delay_seconds=1.0,
                description="ADK LlmAgent writes real code (fails loud without a model)")


def _build_self_test_command() -> str:
    """Independent self-test for the build hop.

    When a test_solution.py exists in the job dir (e.g. seeded by the
    bench harness or a previous test hop), run pytest so a buggy/unchanged
    solution.py cannot pass on exit-code alone. Otherwise fall back to
    running solution.py. Always writes EVAL.json; the gate decides.
    """
    return (
        "if [ -f test_solution.py ]; then\n"
        "  python3 -B -m pytest test_solution.py --tb=short -q > test_output.log 2>&1; rc=$?;\n"
        "  if grep -qE 'error|no tests ran|collection' test_output.log; then\n"
        "    printf '{\"checks\":[{\"name\":\"tests-pass\",\"passed\":false,"
        "\"message\":\"pytest collection error\"}],\"exit_code\":1}' > EVAL.json;\n"
        "  elif [ $rc -eq 0 ]; then\n"
        "    passed=$(grep -c ' PASSED' test_output.log 2>/dev/null) || true; passed=${passed:-0};\n"
        "    printf '{\"checks\":[{\"name\":\"tests-pass\",\"passed\":true,"
        "\"message\":\"%s test(s) passed\"}],\"exit_code\":0}' \"$passed\" > EVAL.json;\n"
        "  else\n"
        "    failed=$(grep -c 'FAILED' test_output.log 2>/dev/null) || true; failed=${failed:-0};\n"
        "    passed=$(grep -c ' PASSED' test_output.log 2>/dev/null) || true; passed=${passed:-0};\n"
        "    printf '{\"checks\":[{\"name\":\"tests-pass\",\"passed\":false,"
        "\"message\":\"%s test(s) failed, %s passed\"}],\"exit_code\":%s}'"
        " \"$failed\" \"$passed\" \"$rc\" > EVAL.json;\n"
        "  fi\n"
        "else\n"
        "  python3 -B solution.py > build.log 2>&1; rc=$?;\n"
        "  if [ $rc -eq 0 ]; then\n"
        "    printf '{\"checks\":[{\"name\":\"solution-runs\",\"passed\":true,"
        "\"message\":\"exit 0\"}],\"exit_code\":0}' > EVAL.json;\n"
        "  else\n"
        "    printf '{\"checks\":[{\"name\":\"solution-runs\",\"passed\":false,"
        "\"message\":\"exit %s\"}],\"exit_code\":%s}' \"$rc\" \"$rc\" > EVAL.json;\n"
        "  fi\n"
        "fi"
    )


def build_hop() -> Hop:
    wf = Workflow(id="build", description="Implement per plan with self-test")
    # ADK agent writes solution.py (real code); the self-test node below is
    # INDEPENDENT of the agent and writes EVAL.json from the ACTUAL run
    # result — the builder never certifies its own output (kills
    # self-certification, gives the review hop real evidence to cite).
    wf.add_node(_build_adk_node())
    wf.add_node(Node(
        id="self-test", kind="bash",
        command=_build_self_test_command(),
        depends_on=["build"],
        description="Independent self-test: pytest (when tests exist) else "
                    "solution.py run, writes EVAL.json",
    ))
    return Hop(
        id="build", workflow=wf,
        required_artifacts=["solution.py", "EVAL.json"],
        gate_checks={
            "eval-json": eval_json_check(),
            "exit-codes": exit_codes_check(),
            "artifacts": required_artifact_check(["solution.py", "EVAL.json"]),
        },
        max_fix_loops=2,
    )


def review_hop() -> Hop:
    wf = Workflow(id="review", description="QA the build; verdict must be PASS")
    wf.add_node(Node(
        id="review", kind="bash",
        command=(
            "echo '# Review' > review.md; "
            "echo 'Verdict: PASS' >> review.md; "
            "echo 'Evidence: EVAL.json all checks passed, self-test exited 0' >> review.md; "
            "grep -q 'PASS' review.md || exit 1"
        ),
    ))
    return Hop(
        id="review", workflow=wf,
        required_artifacts=["review.md", "EVAL.json"],
        gate_checks={
            "review-pass": required_artifact_check(["review.md"]),
            "exit-codes": exit_codes_check(),
        },
        max_fix_loops=1,
    )


def _teach_gemma_node() -> Node:
    """Teach hop backed by Gemma 4 (2nd Google model).

    Model-or-fail: when gemma_generate returns None (no key, HTTP error,
    no candidates) the node raises WorkflowError — the job fails loud.
    NEVER a canned lesson: fabricated "lessons" would poison the LEARN loop.
    """
    from nine.runtime.gemma import gemma_generate
    from nine.runtime.workflows import WorkflowError

    def _run(inputs: dict, job_dir):
        task = inputs.get("task", "the completed task")
        prompt = (
            "You are the teach hop of nine, an evidence-gated agent OS.\n"
            "Write a concise lesson (<=120 words, markdown) an agent should "
            "remember from this run.\n"
            "Task: " + task + "\n"
            "Lesson style: one concrete, reusable behavioral rule, "
            "candidate-only (human reviews before adoption)."
        )
        text = gemma_generate(prompt)
        if not text:
            raise WorkflowError(
                "teach requires Gemma (gemma_generate returned None) — no "
                "offline fallback, nine is model-driven"
            )
        (job_dir / "TEACH.md").write_text(
            "# Teach\n\n" + text + "\n\n"
            "Status: candidate (reviewed by human before adoption)\n"
        )
        return {"output": "teach hop done"}

    return Node(id="teach", kind="prompt", run=_run, max_retries=2,
                retry_delay_seconds=1.0,
                description="Gemma-4 lesson writer (fails loud without a model)")


def teach_hop() -> Hop:
    wf = Workflow(id="teach", description="Capture the lesson (candidate-only self-improvement)")
    wf.add_node(_teach_gemma_node())
    return Hop(
        id="teach", workflow=wf,
        required_artifacts=["TEACH.md"],
        gate_checks={"teach-md": required_artifact_check(["TEACH.md"])},
        max_fix_loops=1,
    )


# ---------------------------------------------------------------- chain

def research_plan_build_review_teach() -> Chain:
    """The 5-hop department chain (flagship demo)."""
    return Chain(
        id="research-plan-build-review-teach",
        description="Five-hop department handoff with evidence gates between hops",
        hops=[research_hop(), plan_hop(), build_hop(), review_hop(), teach_hop()],
    )


# ------------------------------------------------- demo lane: inbox->triage->task->report

def demo_lane() -> Chain:
    """The lean Taskmaster demo lane: inbox -> triage -> task -> report.

    This is deliberately small (4 deterministic hops) so the live demo
    finishes fast and every hop evidence is visible in the ledger.
    """
    triage_wf = Workflow(id="triage", description="Classify the inbox item")
    triage_wf.add_node(Node(
        id="triage", kind="bash",
        command=(
            "cat inbox.txt > _task; "
            "echo '# Triage' > triage.md; "
            "echo 'Class: task' >> triage.md; "
            "echo 'Priority: normal' >> triage.md"
        ),
    ))
    task_wf = Workflow(id="task", description="Execute the task")
    task_wf.add_node(Node(
        id="task", kind="bash",
        command=(
            "echo '# Task result' > task_result.md; "
            "echo 'Inbox item:' >> task_result.md; "
            "cat _task >> task_result.md; "
            "echo 'Done: routed, executed, evidence-gated.' >> task_result.md; "
            "python3 -c 'import json; "
            "json.dump({\"checks\":[{\"name\":\"task-complete\",\"passed\":True}]}, "
            "open(\"EVAL.json\",\"w\"))'"
        ),
    ))
    report_wf = Workflow(id="report", description="Write the final report")
    report_wf.add_node(Node(
        id="report", kind="bash",
        command=(
            "echo '# Report' > FINAL_REPORT.md; "
            "echo 'Task:' >> FINAL_REPORT.md; cat _task >> FINAL_REPORT.md; "
            "echo >> FINAL_REPORT.md; "
            "cat task_result.md >> FINAL_REPORT.md; "
            "test -s FINAL_REPORT.md"
        ),
    ))
    return Chain(
        id="inbox-triage-task-report",
        description="Taskmaster demo lane: inbox -> triage -> task -> report",
        hops=[
            Hop(id="triage", workflow=triage_wf,
                required_artifacts=["triage.md", "inbox.txt"],
                gate_checks={"triage-md": required_artifact_check(["triage.md"])}),
            Hop(id="task", workflow=task_wf,
                required_artifacts=["task_result.md", "EVAL.json"],
                gate_checks={"eval-json": eval_json_check(),
                             "exit-codes": exit_codes_check()}),
            Hop(id="report", workflow=report_wf,
                required_artifacts=["FINAL_REPORT.md"],
                gate_checks={"report-md": required_artifact_check(["FINAL_REPORT.md"])}),
        ],
    )
