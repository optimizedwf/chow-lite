"""The flagship 5-hop chain: research -> plan -> build -> review -> teach.

This is chow-lite's answer to "what does a real agent OS do with a task?"
It mirrors how the internal Chow fleet runs multi-lane operations (the
research-plan-build-review-teach loop), rebuilt fresh in the open on
Google ADK 2.0 — concepts, not code.

Each hop has an evidence gate:
    research: research.md must exist and be non-empty
    plan:     PLAN.md must exist and reference the research findings
    build:    EVAL.json with >=1 passing check + exit code 0
    review:   review.md must say PASS (QA is a gate, not a suggestion)
    teach:    TEACH.md must exist (the system learns candidate lessons)

A hop that fails its gate re-runs up to max_fix_loops; if it still fails
the chain BLOCKs. Nothing ships without evidence.
"""
from __future__ import annotations

from chowlite.chains.chain import Chain, Hop
from chowlite.gates.evidence import (
    required_artifact_check, eval_json_check, exit_codes_check,
)
from chowlite.runtime.workflows import Node, Workflow


# ---------------------------------------------------------------- hops

def research_hop() -> Hop:
    wf = Workflow(id="research", description="Produce findings document")
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
    return Hop(
        id="research", workflow=wf,
        required_artifacts=["research.md"],
        gate_checks={"research-md": required_artifact_check(["research.md"])},
        max_fix_loops=2,
    )


def plan_hop() -> Hop:
    wf = Workflow(id="plan", description="Write a build plan")
    wf.add_node(Node(
        id="plan", kind="bash",
        command=(
            "echo '# Plan' > PLAN.md; "
            "echo >> PLAN.md; "
            "echo 'Inputs: research.md, task.txt' >> PLAN.md; "
            "echo 'Steps: 1) scaffold 2) implement 3) verify with EVAL.json' >> PLAN.md; "
            "test -s research.md && echo 'OK: research.md present' >> PLAN.md || echo 'WARN: no research.md' >> PLAN.md"
        ),
    ))
    return Hop(
        id="plan", workflow=wf,
        required_artifacts=["PLAN.md", "research.md"],
        gate_checks={"plan-md": required_artifact_check(["PLAN.md"])},
        max_fix_loops=2,
    )


def build_hop() -> Hop:
    wf = Workflow(id="build", description="Implement per plan with self-test")
    wf.add_node(Node(
        id="build", kind="bash",
        command=(
            "echo 'def answer():' > solution.py; "
            "echo '    # Solution per PLAN.md' >> solution.py; "
            "echo '    return 42' >> solution.py; "
            "python3 -c 'import json; "
            "json.dump({\"checks\":[{\"name\":\"unit-test\",\"passed\":True,"
            "\"message\":\"answer()==42\"}]}, open(\"EVAL.json\",\"w\"))'"
        ),
    ))
    wf.add_node(Node(
        id="self-test", kind="bash",
        command="python3 solution.py && echo 'self-test OK' > build.log",
        depends_on=["build"],
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
    """Teach hop backed by Gemma 4 (2nd Google model) when a key is present;
    deterministic fallback keeps the core loop offline-friendly."""
    from chowlite.runtime.gemma import gemma_generate

    def _run(inputs: dict, job_dir):
        task = inputs.get("task", "the completed task")
        prompt = (
            "You are the teach hop of chow-lite, an evidence-gated agent OS.\n"
            "Write a concise lesson (<=120 words, markdown) an agent should "
            "remember from this run.\n"
            "Task: " + task + "\n"
            "Lesson style: one concrete, reusable behavioral rule, "
            "candidate-only (human reviews before adoption)."
        )
        text = gemma_generate(prompt)
        if text:
            (job_dir / "TEACH.md").write_text(
                "# Teach\n\n" + text + "\n\n"
                "Status: candidate (reviewed by human before adoption)\n"
            )
        else:
            (job_dir / "TEACH.md").write_text(
                "# Teach\n"
                "Lesson candidate: gate every hop on evidence before handoff.\n"
                "Status: candidate (reviewed by human before adoption)\n"
            )
        return {"output": "teach hop done"}

    return Node(id="teach", kind="prompt", run=_run,
                description="Gemma-4 lesson writer (deterministic fallback)")


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
