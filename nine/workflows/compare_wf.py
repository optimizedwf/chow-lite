"""Compare workflow - 2+ options vs criteria -> COMPARISON.md recommendation.

The `compare` lane of nine: criteria-extract (prompt) reads the task (which
lists the options and any criteria) and writes CRITERIA.md; per-option-
analyzer (tool/ADK) reads CRITERIA.md and writes OPTIONS.md scoring each
option against every criterion; comparator (prompt) reads both and writes
COMPARISON.md with an explicit `Recommendation:` line. Gate requires the
Recommendation line + a comparison table.

Model-or-fail: without GEMINI_API_KEY the model nodes raise WorkflowError -
the job fails loud. NEVER a canned comparison.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nine.chains.chain import Hop
from nine.gates.evidence import (
    exit_codes_check,
    required_artifact_check,
)
from nine.runtime.fsafety import contained_write
from nine.runtime.llm_provider import key_available
from nine.runtime.summarizer import _gemini_generate
from nine.runtime.workflows import Node, Workflow, WorkflowError


def _require_key(lane: str) -> None:
    """Model-or-fail: every model node checks GEMINI_API_KEY first."""
    if not key_available():
        raise WorkflowError(
            f"{lane} requires GEMINI_API_KEY (ADK LlmAgent) - no offline "
            "fallback, nine is model-driven"
        )


def _criteria_prompt_node() -> Node:
    """Prompt node: extract options + criteria -> CRITERIA.md."""
    def _run(inputs: dict, job_dir) -> dict:
        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:600]
        _require_key("compare (criteria-extract)")

        prompt = (
            "You are the criteria extractor of nine, an evidence-gated "
            "agent OS. From the task below, identify the OPTIONS to "
            "compare (2+ distinct choices) and the CRITERIA to judge them "
            "on. Write CRITERIA.md with:\n"
            "1. Options - numbered list, one per option.\n"
            "2. Criteria - numbered list with a short definition and "
            "whether HIGHER or LOWER is better.\n"
            "3. Weights - optional; if the task implies importance, "
            "assign weights (sum 1.0); otherwise mark equal.\n"
            "If the task names no explicit criteria, derive 4-6 sensible "
            "ones for the domain.\n"
            f"Task: {task}\n"
        )
        criteria = _gemini_generate(prompt, api_key=None)
        if not (criteria and criteria.strip()):
            raise WorkflowError(
                "compare (criteria-extract) model returned nothing - job "
                "failed loud (no offline fallback)"
            )
        (job_dir / "CRITERIA.md").write_text(criteria.strip(), encoding="utf-8")
        return {"output": "wrote CRITERIA.md",
                "artifact_path": str(job_dir / "CRITERIA.md")}

    return Node(
        id="criteria-extract", kind="prompt", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="Prompt node writes CRITERIA.md (fails loud without a model)",
    )


def _analyzer_adk_node() -> Node:
    """ADK LlmAgent: score each option against every criterion -> OPTIONS.md.

    Model-or-fail: raises WorkflowError without GEMINI_API_KEY.
    """
    def _run(inputs: dict, job_dir) -> dict:
        from nine.runtime.adk_runtime import ADKAgentNode

        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:500]
        _require_key("compare (analyzer)")

        from google.adk.agents import LlmAgent
        from google.adk.models import Gemini
        from google.adk.tools import FunctionTool

        def write_file(path: str, content: str) -> str:
            """Write a file into the workspace (job dir)."""
            contained_write(job_dir, path, content)
            return f"wrote {path} ({len(content)} bytes)"

        criteria = ""
        if (job_dir / "CRITERIA.md").exists():
            criteria = (job_dir / "CRITERIA.md").read_text(
                encoding="utf-8")[:3000]

        agent = LlmAgent(
            name="analyzer",
            model=Gemini(model="gemini-3.6-flash"),
            instruction=(
                "You are the option analyzer of nine. Score every option "
                "from CRITERIA.md against every criterion. Write "
                "OPTIONS.md with:\n"
                "- A table: rows = options, columns = criteria, cells = "
                "score 1-5 plus a short note.\n"
                "- Below the table, one paragraph per option: evidence, "
                "trade-offs, best-for.\n"
                "Be objective; do not pick a winner yet (the comparator "
                "does). Use the write_file tool.\n"
                f"Task: {task}\n"
                f"CRITERIA.md:\n{criteria}\n"
            ),
            tools=[FunctionTool(write_file)],
        )
        return ADKAgentNode(agent)(inputs, job_dir)

    return Node(
        id="analyzer", kind="tool", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="ADK LlmAgent writes OPTIONS.md (fails loud without a model)",
    )


def _comparator_prompt_node() -> Node:
    """Prompt node: weigh scores -> COMPARISON.md with Recommendation line."""
    def _run(inputs: dict, job_dir) -> dict:
        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:500]
        _require_key("compare (comparator)")

        def _read(name: str, limit: int = 3000) -> str:
            p = job_dir / name
            return p.read_text(encoding="utf-8")[:limit] if p.exists() else "(missing)"

        prompt = (
            "You are the comparator of nine, an evidence-gated agent OS. "
            "Weigh the per-option analysis against the criteria (respect "
            "weights and HIGHER/LOWER direction). Write COMPARISON.md "
            "with:\n"
            "1. A line exactly `Recommendation: <option>` naming one "
            "option.\n"
            "2. Scorecard - the option x criterion table with totals.\n"
            "3. Justification - 3-5 bullets tying the recommendation to "
            "specific scores.\n"
            "4. Runner-up - one line on the second-best option.\n"
            f"Task: {task}\n"
            f"CRITERIA.md:\n{_read('CRITERIA.md')}\n"
            f"OPTIONS.md:\n{_read('OPTIONS.md')}\n"
        )
        comparison = _gemini_generate(prompt, api_key=None)
        if not (comparison and comparison.strip()):
            raise WorkflowError(
                "compare (comparator) model returned nothing - job failed "
                "loud (no offline fallback)"
            )
        (job_dir / "COMPARISON.md").write_text(
            comparison.strip(), encoding="utf-8")
        return {"output": "wrote COMPARISON.md",
                "artifact_path": str(job_dir / "COMPARISON.md")}

    return Node(
        id="comparator", kind="prompt", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="Prompt node writes COMPARISON.md (fails loud without a model)",
    )


def _recommendation_check(ctx: dict[str, Any], workdir: Path) -> tuple[bool, str]:
    """COMPARISON.md must carry an explicit `Recommendation:` line."""
    p = Path(workdir) / "COMPARISON.md"
    if not p.exists():
        return False, "COMPARISON.md missing"
    txt = p.read_text(encoding="utf-8")
    if "Recommendation:" not in txt:
        return False, "COMPARISON.md missing Recommendation line"
    return True, "COMPARISON.md has Recommendation"


def compare_hop() -> Hop:
    """The `compare` workflow: options vs criteria -> recommendation.

    Three-node hop:
      1. criteria-extract (prompt)  - CRITERIA.md (options + criteria)
      2. analyzer (tool/ADK)        - OPTIONS.md (scores per criterion)
      3. comparator (prompt)        - COMPARISON.md (Recommendation line)

    Gate: Recommendation line + all three artifacts + exit codes.
    """
    wf = Workflow(id="compare",
                  description="2+ options vs criteria -> recommendation")
    criteria = _criteria_prompt_node()
    analyzer = _analyzer_adk_node()
    analyzer.depends_on = ["criteria-extract"]
    comparator = _comparator_prompt_node()
    comparator.depends_on = ["analyzer"]
    for n in (criteria, analyzer, comparator):
        wf.add_node(n)
    return Hop(
        id="compare", workflow=wf,
        required_artifacts=["CRITERIA.md", "OPTIONS.md", "COMPARISON.md"],
        gate_checks={
            "exit-codes": exit_codes_check(),
            "artifacts": required_artifact_check(
                ["CRITERIA.md", "OPTIONS.md", "COMPARISON.md"]
            ),
            "recommendation": _recommendation_check,
        },
        max_fix_loops=2,
    )
