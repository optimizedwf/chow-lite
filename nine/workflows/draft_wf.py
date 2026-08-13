"""Draft workflow - spec/proposal/article with draft->review->revise loop.

The `draft` lane of nine: drafter (tool/ADK) writes DRAFT.md v1 from the
task (topic/spec/audience); reviewer (prompt) reads it and writes
REVIEW.md (numbered findings + requested changes); reviser (tool/ADK)
reads both, appends REVISION_LOG.md entries (review point -> action) and
rewrites DRAFT.md as the final version. Gate requires a non-trivial
DRAFT.md and a revision log with at least one entry.

Model-or-fail: without GEMINI_API_KEY the model nodes raise WorkflowError -
the job fails loud. NEVER a canned draft.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from nine.chains.chain import Hop
from nine.gates.evidence import (
    exit_codes_check,
    file_nonempty_check,
    required_artifact_check,
)
from nine.runtime.fsafety import contained_write
from nine.runtime.summarizer import _gemini_generate
from nine.runtime.workflows import Node, Workflow, WorkflowError


def _require_key(lane: str) -> None:
    """Model-or-fail: every model node checks GEMINI_API_KEY first."""
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        raise WorkflowError(
            f"{lane} requires GEMINI_API_KEY (ADK LlmAgent) - no offline "
            "fallback, nine is model-driven"
        )


def _draft_adk_node() -> Node:
    """ADK LlmAgent: write DRAFT.md v1 from the task."""
    def _run(inputs: dict, job_dir) -> dict:
        from nine.runtime.adk_runtime import ADKAgentNode

        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:600]
        _require_key("draft (drafter)")

        from google.adk.agents import LlmAgent
        from google.adk.models import Gemini
        from google.adk.tools import FunctionTool

        def write_file(path: str, content: str) -> str:
            """Write a file into the workspace (job dir)."""
            contained_write(job_dir, path, content)
            return f"wrote {path} ({len(content)} bytes)"

        agent = LlmAgent(
            name="drafter",
            model=Gemini(model="gemini-3.6-flash"),
            instruction=(
                "You are the drafter of nine, an evidence-gated agent OS. "
                "Write a first draft DRAFT.md of the requested artifact "
                "(proposal, spec, article, plan) using the write_file "
                "tool.\n"
                "Rules:\n"
                "- Structure it for the stated purpose and audience; "
                "include a title (H1) and clear sections.\n"
                "- Be concrete and specific - no filler; where numbers or "
                "facts are needed, mark them [TBD] for the reviewer to "
                "flag rather than inventing them.\n"
                "- Aim for a complete, self-contained first pass.\n"
                f"Task: {task}\n"
            ),
            tools=[FunctionTool(write_file)],
        )
        return ADKAgentNode(agent)(inputs, job_dir)

    return Node(
        id="draft", kind="tool", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="ADK LlmAgent writes DRAFT.md (fails loud without a model)",
    )


def _review_prompt_node() -> Node:
    """Prompt node: critique DRAFT.md -> REVIEW.md (findings + changes)."""
    def _run(inputs: dict, job_dir) -> dict:
        job_dir = Path(job_dir)
        _require_key("draft (reviewer)")

        draft = ""
        p = job_dir / "DRAFT.md"
        if p.exists():
            draft = p.read_text(encoding="utf-8")[:4000]

        prompt = (
            "You are the reviewer of nine. Critique the draft below for "
            "clarity, completeness, structure, and factual safety. Write "
            "REVIEW.md with:\n"
            "1. Summary - 2-3 lines on what the draft does well.\n"
            "2. Findings - numbered list; each item: issue, why it "
            "matters, requested change.\n"
            "3. Priority - tag each finding MUST / SHOULD / NICE.\n"
            "4. Verdict line: `Verdict: REVISE` or `Verdict: APPROVE`.\n"
            "Be demanding but constructive.\n\n"
            f"DRAFT.md:\n{draft}\n"
        )
        review = _gemini_generate(prompt, api_key=None)
        if not (review and review.strip()):
            raise WorkflowError(
                "draft (reviewer) model returned nothing - job failed "
                "loud (no offline fallback)"
            )
        (job_dir / "REVIEW.md").write_text(review.strip(), encoding="utf-8")
        return {"output": "wrote REVIEW.md",
                "artifact_path": str(job_dir / "REVIEW.md")}

    return Node(
        id="review", kind="prompt", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="Prompt node writes REVIEW.md (fails loud without a model)",
    )


def _revise_adk_node() -> Node:
    """ADK LlmAgent: apply the review -> final DRAFT.md + REVISION_LOG.md."""
    def _run(inputs: dict, job_dir) -> dict:
        from nine.runtime.adk_runtime import ADKAgentNode

        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:400]
        _require_key("draft (reviser)")

        from google.adk.agents import LlmAgent
        from google.adk.models import Gemini
        from google.adk.tools import FunctionTool

        def write_file(path: str, content: str) -> str:
            """Write a file into the workspace (job dir)."""
            contained_write(job_dir, path, content)
            return f"wrote {path} ({len(content)} bytes)"

        draft = ""
        p = job_dir / "DRAFT.md"
        if p.exists():
            draft = p.read_text(encoding="utf-8")[:4000]
        review = ""
        p = job_dir / "REVIEW.md"
        if p.exists():
            review = p.read_text(encoding="utf-8")[:3000]

        agent = LlmAgent(
            name="reviser",
            model=Gemini(model="gemini-3.6-flash"),
            instruction=(
                "You are the reviser of nine. Revise DRAFT.md to address "
                "every MUST and SHOULD finding in REVIEW.md (NICE ones at "
                "your discretion).\n"
                "1. Overwrite DRAFT.md with the final revised version "
                "using write_file.\n"
                "2. Append to REVISION_LOG.md (create it if missing) "
                "one entry per addressed finding:\n"
                "   `- [x] Finding N: <issue> -> <what you changed>`\n"
                "   and for any skipped finding:\n"
                "   `- [ ] Finding N: <issue> -> <why skipped>`\n"
                f"Task: {task}\n"
                f"DRAFT.md:\n{draft}\n"
                f"REVIEW.md:\n{review}\n"
            ),
            tools=[FunctionTool(write_file)],
        )
        return ADKAgentNode(agent)(inputs, job_dir)

    return Node(
        id="revise", kind="tool", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="ADK LlmAgent finalizes DRAFT.md + REVISION_LOG.md "
                    "(fails loud without a model)",
    )


def _revision_log_check(ctx: dict[str, Any], workdir: Path) -> tuple[bool, str]:
    """REVISION_LOG.md must carry at least one revision entry."""
    p = Path(workdir) / "REVISION_LOG.md"
    if not p.exists():
        return False, "REVISION_LOG.md missing"
    txt = p.read_text(encoding="utf-8").strip()
    if len(txt) < 10:
        return False, "REVISION_LOG.md is empty"
    return True, "REVISION_LOG.md has revision entries"


def draft_hop() -> Hop:
    """The `draft` workflow: draft -> review -> revise.

    Three-node hop:
      1. draft (tool/ADK)   - DRAFT.md v1
      2. review (prompt)    - REVIEW.md (findings + verdict)
      3. revise (tool/ADK)  - final DRAFT.md + REVISION_LOG.md

    Gate: non-empty DRAFT.md + revision log + all artifacts + exit codes.
    """
    wf = Workflow(id="draft",
                  description="Draft -> review -> revise loop")
    drafter = _draft_adk_node()
    reviewer = _review_prompt_node()
    reviewer.depends_on = ["draft"]
    reviser = _revise_adk_node()
    reviser.depends_on = ["review"]
    for n in (drafter, reviewer, reviser):
        wf.add_node(n)
    return Hop(
        id="draft", workflow=wf,
        required_artifacts=["DRAFT.md", "REVIEW.md", "REVISION_LOG.md"],
        gate_checks={
            "exit-codes": exit_codes_check(),
            "artifacts": required_artifact_check(
                ["DRAFT.md", "REVIEW.md", "REVISION_LOG.md"]
            ),
            "nonempty": file_nonempty_check("DRAFT.md", min_chars=100),
            "revision-log": _revision_log_check,
        },
        max_fix_loops=2,
    )
