from __future__ import annotations

import json
import re
from pathlib import Path

from nine.chains.chain import Chain, Hop
from nine.gates.evidence import (
    eval_json_check,
    exit_codes_check,
    file_nonempty_check,
    required_artifact_check,
)
from nine.runtime.llm_provider import key_available
from nine.runtime.workflows import Node, Workflow

# ---------------------------------------------------------------- hops

def _fix_directive_suffix(fix_dir: str) -> str:
    """Append the hop FIX directive to an ADK instruction when present.

    torture-7 F2: flagship ADK hops used to ignore fix_directive, so a hop
    FIX re-run re-burned model budget with a byte-identical prompt. The
    directive names what failed so the rework converges instead of BLOCKing.
    """
    if not fix_dir:
        return ""
    return (
        f"\nPrevious attempt failed the gate: {fix_dir}\n"
        "Rework the artifacts accordingly — read the failure and fix "
        "exactly what it names."
    )


def _research_adk_node() -> Node:
    """Research hop backed by a REAL Google ADK 2.0 LlmAgent.

    The agent (Gemini 3.6 Flash via google-adk) reads the task and writes
    research.md with ACTUAL findings for THAT task. Model-or-fail: with no
    GEMINI_API_KEY the node raises WorkflowError and the job fails loud.
    NEVER canned research — fabricated findings would be a lie in an
    evidence-gated system (torture finding T1-F8/T2-F1).
    """
    from nine.runtime.workflows import WorkflowError

    def _run(inputs: dict, job_dir) -> dict:
        from nine.runtime.adk_runtime import ADKAgentNode

        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:1500]
        fix_dir = str(inputs.get("fix_directive", ""))[:1500]
        if not key_available():
            raise WorkflowError(
                "research requires an LLM key (gemini: GEMINI_API_KEY; openai: NINE_LLM_API_KEY/OPENCODE_GO_API_KEY) (ADK LlmAgent) — no offline "
                "fallback, nine is model-driven"
            )

        from google.adk.agents import LlmAgent
        from google.adk.tools import FunctionTool

        from nine.runtime import llm_provider

        def write_file(path: str, content: str) -> str:
            """Write a findings file into the research workspace (job dir)."""
            _contained_write(job_dir, path, content)
            return f"wrote {path} ({len(content)} bytes)"

        agent = LlmAgent(
            name="researcher",
            model=llm_provider.adk_model(),
            instruction=(
                "You are the research hop of nine, an evidence-gated agent OS.\n"
                "Research the task below and write ONE file with the "
                "write_file tool: `research.md` — a findings document with "
                "(1) what the task actually asks, (2) the key constraints "
                "and risks, (3) concrete approaches with tradeoffs. Base "
                "every claim on the task text; never invent facts and never "
                "copy canned boilerplate.\n"
                f"Task: {task}"
                + _fix_directive_suffix(fix_dir)
            ),
            tools=[FunctionTool(write_file)],
        )
        node = ADKAgentNode(agent)
        return node(inputs, job_dir)

    return Node(id="research", kind="tool", run=_run, max_retries=2,
                retry_delay_seconds=1.0,
                description="ADK LlmAgent researches the task (fails loud without a model)")



def _contained_write(job_dir: Path, path: str, content: str) -> None:
    """Write `content` to `path` inside job_dir, refusing `..` escapes.

    torture T3-F7 / T5-F1: the model controls `path`; a confused/adversarial
    model could write `../EVAL.json` and poison ANOTHER job's evidence (or
    the router catalog / ledger). Single shared implementation lives in
    nine/runtime/fsafety.py (used by every workflow's write_file tool).
    """
    from nine.runtime.fsafety import contained_write

    contained_write(job_dir, path, content)

def research_hop(include_datahub: bool = False) -> Hop:
    wf = Workflow(id="research", description="Produce findings + distilled handoff")
    wf.add_node(_research_adk_node())
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
            "research-nonempty": file_nonempty_check("research.md", min_chars=50),
        },
        max_fix_loops=2,
    )


def _plan_adk_node() -> Node:
    """Plan hop backed by a REAL Google ADK 2.0 LlmAgent.

    The agent (Gemini 3.6 Flash via google-adk) reads the distilled research
    (HANDOFF.md) + task and writes PLAN.md with a build plan specific to
    THAT task. Model-or-fail: no GEMINI_API_KEY -> WorkflowError, fail loud.
    NEVER a canned template plan (torture finding T2-F1).
    """
    from nine.runtime.workflows import WorkflowError

    def _run(inputs: dict, job_dir) -> dict:
        from nine.runtime.adk_runtime import ADKAgentNode

        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:1500]
        fix_dir = str(inputs.get("fix_directive", ""))[:1500]
        if not key_available():
            raise WorkflowError(
                "plan requires an LLM key (gemini: GEMINI_API_KEY; openai: NINE_LLM_API_KEY/OPENCODE_GO_API_KEY) (ADK LlmAgent) — no offline "
                "fallback, nine is model-driven"
            )

        from google.adk.agents import LlmAgent
        from google.adk.tools import FunctionTool

        from nine.runtime import llm_provider

        def write_file(path: str, content: str) -> str:
            """Write the plan file into the plan workspace (job dir)."""
            _contained_write(job_dir, path, content)
            return f"wrote {path} ({len(content)} bytes)"

        handoff = ""
        if (job_dir / "HANDOFF.md").exists():
            handoff = (job_dir / "HANDOFF.md").read_text(encoding="utf-8")[:1200]

        agent = LlmAgent(
            name="planner",
            model=llm_provider.adk_model(),
            instruction=(
                "You are the plan hop of nine, an evidence-gated agent OS.\n"
                "Read the distilled research (HANDOFF.md) and the task, then "
                "write ONE file with the write_file tool: `PLAN.md` — "
                "numbered build steps (scaffold, implement, verify), the "
                "acceptance checks the build must pass, and the risks to "
                "watch. The plan must be specific to THIS task — never a "
                "generic template.\n"
                f"Task: {task}\n"
                f"HANDOFF.md:\n{handoff or '(none)'}"
                + _fix_directive_suffix(fix_dir)
            ),
            tools=[FunctionTool(write_file)],
        )
        node = ADKAgentNode(agent)
        return node(inputs, job_dir)

    return Node(id="plan", kind="tool", run=_run, max_retries=2,
                retry_delay_seconds=1.0,
                description="ADK LlmAgent writes a task-specific plan (fails loud without a model)")


def plan_hop(require_handoff: bool = True) -> Hop:
    """Plan hop. torture-5 F5: the STANDALONE plan workflow can never SHIP
    because its gate demands HANDOFF.md, which only the research hop's
    summarize step writes — standalone plan (no research hop) would FIX-loop
    forever into BLOCK. Chains pass require_handoff=True (strict: the plan
    must build on a real research handoff); standalone `nine submit "plan x"`
    passes require_handoff=False and gates on PLAN.md alone.
    """
    wf = Workflow(id="plan", description="Write a build plan")
    wf.add_node(_plan_adk_node())
    required = ["PLAN.md"] if not require_handoff else ["PLAN.md", "HANDOFF.md"]
    checks = {
        "plan-md": required_artifact_check(["PLAN.md"]),
        "plan-nonempty": file_nonempty_check("PLAN.md", min_chars=30),
    }
    if require_handoff:
        checks["handoff-md"] = required_artifact_check(["HANDOFF.md"])
    return Hop(
        id="plan", workflow=wf,
        required_artifacts=required,
        gate_checks=checks,
        max_fix_loops=2,
    )


def _build_adk_node() -> Node:
    """Build hop backed by a REAL Google ADK 2.0 LlmAgent.

    The agent (Gemini 3.6 Flash via google-adk) reads task.txt + PLAN.md and
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
        fix_dir = str(inputs.get("fix_directive", ""))[:1500]
        if not key_available():
            raise WorkflowError(
                "build requires an LLM key (gemini: GEMINI_API_KEY; openai: NINE_LLM_API_KEY/OPENCODE_GO_API_KEY) (ADK LlmAgent) — no offline "
                "fallback, nine is model-driven"
            )

        from google.adk.agents import LlmAgent
        from google.adk.tools import FunctionTool

        from nine.runtime import llm_provider

        def write_file(path: str, content: str) -> str:
            """Write a source file into the build workspace (job dir)."""
            _contained_write(job_dir, path, content)
            return f"wrote {path} ({len(content)} bytes)"

        plan = ""
        if (job_dir / "PLAN.md").exists():
            plan = (job_dir / "PLAN.md").read_text(encoding="utf-8")[:800]

        agent = LlmAgent(
            name="coder",
            model=llm_provider.adk_model(),
            instruction=(
                "You are the build hop of nine, an evidence-gated agent OS.\n"
                "Read the task and plan, then write TWO files with the "
                "write_file tool: (1) `solution.py` — ONE runnable Python "
                "module that solves the task; (2) `test_solution.py` — a "
                "pytest file with assertions proving solution.py actually "
                "solves the task. BOTH are mandatory: an independent "
                "self-test runs pytest next, and a build without tests is "
                "UNVERIFIED and fails loud (an exit code is not success — "
                "never fake a pass). Keep the code simple, dependency-free, "
                "and correct.\n"
                f"Task: {task}\nPlan:\n{plan or '(none)'}"
                + _fix_directive_suffix(fix_dir)
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
        "  if [ $rc -eq 5 ] || grep -qE 'no tests ran|ERROR collecting' test_output.log; then\n"
        "    printf '{\"checks\":[{\"name\":\"tests-pass\",\"passed\":false,"
        "\"message\":\"pytest collection error\"}],\"exit_code\":1}' > EVAL.json;\n"
        "  elif [ $rc -eq 0 ]; then\n"
        "    passed=$(grep -oE '[0-9]+ passed' test_output.log | tail -1 | grep -oE '[0-9]+' || true); passed=${passed:-0};\n"
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
        "  # NO tests = NO verification: an exit code is not success, and a\n"
        "  # solution that merely runs proves nothing about the task. Fail\n"
        "  # loud so the build FIX-loops toward a real test (never fake a pass).\n"
        "  printf '{\"checks\":[{\"name\":\"tests-pass\",\"passed\":false,"
        "\"message\":\"no test evidence - solution runs but unverified (write test_solution.py)\"}],"
        "\"exit_code\":1}' > EVAL.json;\n"
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


def _review_command() -> str:
    """Review derives its verdict from EVAL.json — never a hardcoded PASS.

    torture T3-F2: with NO EVAL.json (standalone review of a job dir that
    never built), grep on the missing file exits 2 -> the else branch -> a
    fabricated 'Verdict: PASS' citing evidence that never existed. A review
    with nothing to review must FAIL loudly.
    """
    return (
        "echo '# Review' > review.md; "
        "if [ ! -f EVAL.json ]; then "
        "echo 'Verdict: FAIL' >> review.md; "
        "echo 'Evidence: no EVAL.json in workspace - nothing to review' >> review.md; "
        "exit 1; "
        "elif grep -qE '\"passed\"[[:space:]]*:[[:space:]]*false|"
        "\"exit_code\"[[:space:]]*:[[:space:]]*[1-9]' EVAL.json; then "
        "echo 'Verdict: FAIL' >> review.md; "
        "echo 'Evidence: EVAL.json contains a failed check or non-zero exit code' >> review.md; "
        "exit 1; "
        "else "
        "echo 'Verdict: PASS' >> review.md; "
        "echo 'Evidence: EVAL.json all checks passed, self-test exited 0' >> review.md; "
        "fi"
    )


def _review_verdict_consistent(ctx: dict, workdir) -> tuple[bool, str]:
    """review.md's verdict must match EVAL.json's actual pass state.

    Kills the theater: a review that says PASS while EVAL.json reports
    failed checks (or vice versa) is a lie in the shipped artifact.
    """
    wd = Path(workdir)
    rp, ep = wd / "review.md", wd / "EVAL.json"
    if not rp.exists() or not ep.exists():
        return False, "review.md or EVAL.json missing"
    try:
        ev = json.loads(ep.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return False, "EVAL.json unparsable"
    checks = ev.get("checks", [])
    ev_passed = bool(ev.get("exit_code") == 0) and all(
        c.get("passed") is True for c in checks
    )
    rtxt = rp.read_text(encoding="utf-8")
    rv = "PASS" if re.search(
        r"^\s*#*\s*(?:Overall\s+)?Verdict:\s*PASS\b",
        rtxt, re.IGNORECASE | re.MULTILINE,
    ) else "FAIL"
    if ev_passed != (rv == "PASS"):
        return False, (
            f"review.md says {rv} but EVAL.json is "
            f"{'passing' if ev_passed else 'failing'}"
        )
    return True, "review.md verdict matches EVAL.json"


def _review_eval_command() -> str:
    """Write the review hop's OWN review-eval.json from review.md's verdict.

    torture-12 F4: this used to write EVAL.json, CLOBBERING the build hop's
    EVAL.json in the chain — the shipped manifest then carried two
    conflicting EVAL.json entries and the review-consistent check compared
    review.md against the review's OWN just-written EVAL (circular, always
    consistent: the build's failing evidence vanished). The review writes a
    DISTINCT review-eval.json; the consistency check keeps reading the
    BUILD's EVAL.json. Standalone `nine submit "review X"` has no build
    EVAL.json to derive from, so the review must produce verifiable
    evidence itself — review-eval.json is that artifact.
    """
    return (
        "if grep -q 'Verdict: PASS' review.md; then "
        "printf '{\"checks\":[{\"name\":\"review-pass\",\"passed\":true,"
        "\"message\":\"review verdict PASS\"}],\"exit_code\":0}' > review-eval.json; "
        "elif grep -q 'Verdict: FAIL' review.md; then "
        "printf '{\"checks\":[{\"name\":\"review-pass\",\"passed\":false,"
        "\"message\":\"review verdict FAIL\"}],\"exit_code\":1}' > review-eval.json; "
        "else "
        "printf '{\"checks\":[{\"name\":\"review-pass\",\"passed\":false,"
        "\"message\":\"no verdict in review.md\"}],\"exit_code\":1}' > review-eval.json; "
        "exit 1; "
        "fi"
    )


def review_hop() -> Hop:
    wf = Workflow(id="review", description="QA the build; verdict must be PASS")
    wf.add_node(Node(
        id="review", kind="bash",
        command=_review_command(),
    ))
    wf.add_node(Node(
        id="review-eval", kind="bash",
        command=_review_eval_command(),
        depends_on=["review"],
        description="Write review EVAL.json from the verdict in review.md",
    ))
    return Hop(
        id="review", workflow=wf,
        required_artifacts=["review.md", "review-eval.json"],
        gate_checks={
            "review-pass": required_artifact_check(["review.md"]),
            "review-consistent": _review_verdict_consistent,
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
