"""Review-multi workflow - 4 parallel reviewer personas merged.

The `review-multi` lane of nine: 4 ADK LlmAgents review a build in
parallel (security, bugs, quality, architecture), each writing a per-dim
report under reviews/; a 5th ADK agent (merger) synthesizes them into
REVIEW.md with an overall verdict. Reference: Archon comprehensive-pr-review
(5 parallel specialists -> synthesize).

Model-or-fail: without GEMINI_API_KEY every ADK node raises WorkflowError -
the job fails loud. NEVER a canned review.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from nine.chains.chain import Hop
from nine.gates.evidence import (
    exit_codes_check,
    required_artifact_check,
)
from nine.runtime.workflows import Node, Workflow

_DIMENSIONS: dict[str, str] = {
    "security": "security vulnerabilities, injection, unsafe eval, secrets, "
                "untrusted input handling",
    "bugs": "logic errors, off-by-one, wrong operators, unhandled exceptions, "
            "incorrect edge cases",
    "quality": "code style, readability, dead code, naming, duplication, "
               "missing error messages",
    "arch": "structure, separation of concerns, coupling, extensibility, "
            "interface design",
}

_DIM_FILES = ["reviews/security.md", "reviews/bugs.md",
              "reviews/quality.md", "reviews/arch.md"]


def _review_verdict_check(ctx: dict[str, Any], workdir: Path) -> tuple[bool, str]:
    """REVIEW.md must carry an explicit Verdict line (PASS or FAIL)."""
    p = Path(workdir) / "REVIEW.md"
    if not p.exists():
        return False, "REVIEW.md missing"
    txt = p.read_text(encoding="utf-8")
    # a verdict line that says FAIL must NOT pass the gate — only an
    # explicit PASS counts as approval (substring "Verdict:" alone let
    # "Verdict: FAIL — ship anyway" SHIP, certifying rejected work).
    if not re.search(
        r"^\s*#+\s*(?:Overall\s+)?Verdict:\s*PASS\b",
        txt, re.IGNORECASE | re.MULTILINE,
    ):
        return False, "REVIEW.md verdict must be PASS (found FAIL or no verdict)"
    return True, "REVIEW.md verdict is PASS"


def _reviewer_adk_node(dimension: str, filename: str) -> Node:
    """One ADK LlmAgent reviewer for a single dimension.

    Reads solution.py (or lists the job dir) and writes
    reviews/<filename> with a per-dim Verdict + findings.
    """
    from nine.runtime.workflows import WorkflowError

    def _run(inputs: dict, job_dir) -> dict:
        from nine.runtime.adk_runtime import ADKAgentNode

        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:200]
        if not os.environ.get("GEMINI_API_KEY"):
            raise WorkflowError(
                f"review-multi ({dimension}) requires GEMINI_API_KEY "
                "(ADK LlmAgent) - no offline fallback, nine is model-driven"
            )

        from google.adk.agents import LlmAgent
        from google.adk.models import Gemini
        from google.adk.tools import FunctionTool

        def write_file(path: str, content: str) -> str:
            """Write a review file into the job dir (reviews/...)."""
            target = job_dir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"wrote {path} ({len(content)} bytes)"

        code = ""
        if (job_dir / "solution.py").exists():
            code = (job_dir / "solution.py").read_text(encoding="utf-8")[:4000]
        else:
            names = sorted(p.name for p in job_dir.iterdir() if p.is_file())
            code = "files in workspace: " + ", ".join(names)

        agent = LlmAgent(
            name=f"{dimension}-reviewer",
            model=Gemini(model="gemini-3.6-flash"),
            instruction=(
                f"You are the {dimension} reviewer of nine, an evidence-gated "
                f"agent OS. Review the code below for {_DIMENSIONS[dimension]}. "
                f"Write reviews/{filename} with: a '## Verdict: PASS or FAIL' "
                "line, "
                "a '## Findings' numbered list (severity, location, "
                "description, concrete suggestion), and a '## Summary' of "
                "1-2 sentences. Be specific and evidence-based - cite line "
                "numbers or symbols. If nothing is wrong in your dimension, "
                "verdict PASS with empty findings.\n"
                f"Task: {task}\n"
                f"Code:\n```python\n{code}\n```"
            ),
            tools=[FunctionTool(write_file)],
        )
        node = ADKAgentNode(agent)
        return node(inputs, job_dir)

    return Node(
        id=f"{dimension}-review", kind="tool", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description=f"ADK LlmAgent reviews for {dimension} (fails loud without a model)",
    )


def _merge_adk_node() -> Node:
    """ADK LlmAgent that synthesizes the 4 per-dim reviews into REVIEW.md."""
    from nine.runtime.workflows import WorkflowError

    def _run(inputs: dict, job_dir) -> dict:
        from nine.runtime.adk_runtime import ADKAgentNode

        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:200]
        if not os.environ.get("GEMINI_API_KEY"):
            raise WorkflowError(
                "review-multi (merge) requires GEMINI_API_KEY (ADK LlmAgent) "
                "- no offline fallback, nine is model-driven"
            )

        from google.adk.agents import LlmAgent
        from google.adk.models import Gemini
        from google.adk.tools import FunctionTool

        def write_file(path: str, content: str) -> str:
            """Write a file into the job dir."""
            target = job_dir / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            return f"wrote {path} ({len(content)} bytes)"

        reports = []
        for rel in _DIM_FILES:
            p = job_dir / rel
            if p.exists():
                reports.append(f"### {rel}\n\n" + p.read_text(encoding="utf-8")[:3000])

        agent = LlmAgent(
            name="review-merger",
            model=Gemini(model="gemini-3.6-flash"),
            instruction=(
                "You are the review merger of nine, an evidence-gated agent "
                "OS. Synthesize the per-dimension review reports below into "
                "REVIEW.md with: a '# Review' title, an '## Overall Verdict: "
                "PASS or FAIL' line (FAIL if ANY dimension verdict is FAIL), "
                "an '## Consolidated Findings' list deduplicated across "
                "dimensions, and '## Per-Dimension Summaries'. Keep the "
                "verdict line exactly in the form '## Overall Verdict: PASS' "
                "or '## Overall Verdict: FAIL'.\n"
                f"Task: {task}\n\n"
                + "\n\n".join(reports)
            ),
            tools=[FunctionTool(write_file)],
        )
        node = ADKAgentNode(agent)
        return node(inputs, job_dir)

    return Node(
        id="merge", kind="tool", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="ADK LlmAgent synthesizes reviews into REVIEW.md (fails loud without a model)",
    )


def review_multi_hop() -> Hop:
    """The `review-multi` workflow: 4 parallel reviewers + merger.

    Hop structure:
      1-4. security/bugs/quality/arch-review (tool/ADK, parallel)
      5.   merge (tool/ADK) - synthesizes REVIEW.md

    Gate: per-dim files + REVIEW.md with a Verdict line must exist.
    """
    wf = Workflow(id="review-multi", description="4-dimensional code review")
    reviewer_ids = []
    for dim, fname in (
        ("security", "security.md"),
        ("bugs", "bugs.md"),
        ("quality", "quality.md"),
        ("arch", "arch.md"),
    ):
        node = _reviewer_adk_node(dim, fname)
        wf.add_node(node)
        reviewer_ids.append(node.id)
    merge_node = _merge_adk_node()
    merge_node.depends_on = reviewer_ids
    wf.add_node(merge_node)
    return Hop(
        id="review-multi", workflow=wf,
        required_artifacts=["REVIEW.md"] + _DIM_FILES,
        gate_checks={
            "verdict": _review_verdict_check,
            "exit-codes": exit_codes_check(),
            "artifacts": required_artifact_check(["REVIEW.md"] + _DIM_FILES),
        },
        max_fix_loops=1,
    )
