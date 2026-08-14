"""Research-quick workflow - single-source quick research (5 min).

The `research-quick` lane of nine: search-prep (prompt) turns the task
into SEARCH_PLAN.md (research question, focus areas, target source,
FINDINGS.md outline); researcher (tool/ADK) reads the plan + the single
source (workspace files) and writes FINDINGS.md with sections; receipt
(bash) writes EVAL.json + RESEARCH_RECEIPT.json. Gate requires FINDINGS.md
with at least two sections.

Model-or-fail: without GEMINI_API_KEY the model nodes raise WorkflowError -
the job fails loud. NEVER a canned research note.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nine.chains.chain import Hop
from nine.gates.evidence import (
    eval_json_check,
    exit_codes_check,
    required_artifact_check,
)
from nine.runtime.fsafety import contained_write
from nine.runtime.llm_provider import key_available
from nine.runtime.summarizer import _gemini_generate
from nine.runtime.workflows import Node, Workflow, WorkflowError


def _search_prep_prompt_node() -> Node:
    """Prompt node: write SEARCH_PLAN.md (focused single-source plan)."""
    def _run(inputs: dict, job_dir) -> dict:
        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:400]
        prompt = (
            "You are the search-prep planner of nine, an evidence-gated "
            "agent OS. Write SEARCH_PLAN.md for a quick single-source "
            "research task (about 5 minutes of work):\n"
            "1. Research Question - restate the task as one answerable "
            "question.\n"
            "2. Focus Areas - 3-5 specific things to look for in the "
            "source.\n"
            "3. Target Source - name the single source to consult "
            "(solution.py / solution/ tree / the files in the workspace).\n"
            "4. FINDINGS.md Outline - the section headers FINDINGS.md "
            "should have (at least: Summary, Details, Evidence, "
            "Recommendations).\n"
            "Be concrete and scoped; this is a 5-minute research task, not "
            "a deep dive.\n"
            f"Task: {task}\n"
        )
        plan = _gemini_generate(prompt, api_key=None)
        if not (plan and plan.strip()):
            raise WorkflowError(
                "research-quick (search-prep) model returned no plan - job "
                "failed loud (no offline fallback)"
            )
        (job_dir / "SEARCH_PLAN.md").write_text(plan.strip(), encoding="utf-8")
        return {"output": "wrote SEARCH_PLAN.md",
                "artifact_path": str(job_dir / "SEARCH_PLAN.md")}

    return Node(
        id="search-prep", kind="prompt", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="Prompt node writes SEARCH_PLAN.md (fails loud without a model)",
    )


def _researcher_adk_node() -> Node:
    """ADK LlmAgent that reads the plan + source and writes FINDINGS.md.

    Model-or-fail: raises WorkflowError without GEMINI_API_KEY.
    """
    def _run(inputs: dict, job_dir) -> dict:
        from nine.runtime.adk_runtime import ADKAgentNode

        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:500]
        if not key_available():
            raise WorkflowError(
                "research-quick (researcher) requires an LLM key (gemini: GEMINI_API_KEY; openai: NINE_LLM_API_KEY/OPENCODE_GO_API_KEY) (ADK "
                "LlmAgent) - no offline fallback, nine is model-driven"
            )

        from google.adk.agents import LlmAgent
        from google.adk.tools import FunctionTool

        from nine.runtime import llm_provider

        def write_file(path: str, content: str) -> str:
            """Write a file into the workspace (job dir)."""
            contained_write(job_dir, path, content)
            return f"wrote {path} ({len(content)} bytes)"

        plan = ""
        if (job_dir / "SEARCH_PLAN.md").exists():
            plan = (job_dir / "SEARCH_PLAN.md").read_text(
                encoding="utf-8")[:3000]

        # Single source: solution.py or the solution/ tree.
        sources = ""
        if (job_dir / "solution.py").exists():
            sources = (job_dir / "solution.py").read_text(
                encoding="utf-8")[:4000]
        else:
            sol = job_dir / "solution"
            if sol.is_dir():
                parts = []
                for p in sorted(sol.rglob("*.py")):
                    if "__pycache__" in str(p):
                        continue
                    parts.append(
                        f"### {p.relative_to(job_dir)}\n"
                        + p.read_text(encoding="utf-8", errors="replace")[:2000]
                    )
                sources = "\n".join(parts)[:6000]
        if not sources:
            sources = "(no source files found in workspace)"

        agent = LlmAgent(
            name="researcher",
            model=llm_provider.adk_model(),
            instruction=(
                "You are the researcher of nine, an evidence-gated agent "
                "OS. Research the single source you are given following "
                "SEARCH_PLAN.md, then write FINDINGS.md with the sections "
                "from the plan outline (at minimum: Summary, Details, "
                "Evidence, Recommendations). Use the write_file tool.\n"
                "Rules: every claim in Details must cite the Evidence "
                "section (file + line or symbol); do not invent facts not "
                "in the source; if the source is missing, say so under "
                "Summary.\n"
                f"Task: {task}\n"
                f"SEARCH_PLAN.md:\n{plan}\n"
                f"Source:\n```python\n{sources}\n```"
            ),
            tools=[FunctionTool(write_file)],
        )
        node = ADKAgentNode(agent)
        return node(inputs, job_dir)

    return Node(
        id="researcher", kind="tool", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="ADK LlmAgent writes FINDINGS.md (fails loud without a model)",
    )


def _receipt_command() -> str:
    """Bash node: certify the research with EVAL.json + RESEARCH_RECEIPT.json.

    Counts FINDINGS.md sections; always exits 0 so the gate decides
    SHIP/FIX from the evidence.
    """
    return r"""
sections=$(grep -c '^## ' FINDINGS.md 2>/dev/null || echo 0)
if [ -f FINDINGS.md ] && [ "$sections" -ge 2 ]; then
  printf '{"checks":[{"name":"findings-sections","passed":true,"message":"%s sections"}],"exit_code":0}' "$sections" > EVAL.json
else
  printf '{"checks":[{"name":"findings-sections","passed":false,"message":"FINDINGS.md missing or <2 sections (got %s)"}],"exit_code":1}' "$sections" > EVAL.json
fi
printf '{"plan":"SEARCH_PLAN.md","findings":"FINDINGS.md","sections":%s}' "$sections" > RESEARCH_RECEIPT.json
exit 0
"""


def _sections_check(ctx: dict[str, Any], workdir: Path) -> tuple[bool, str]:
    """FINDINGS.md must exist with at least two `## ` sections."""
    p = Path(workdir) / "FINDINGS.md"
    if not p.exists():
        return False, "FINDINGS.md missing"
    txt = p.read_text(encoding="utf-8")
    n = sum(1 for line in txt.splitlines() if line.startswith("## "))
    if n < 2:
        return False, f"FINDINGS.md has {n} sections (need >= 2)"
    return True, f"FINDINGS.md has {n} sections"


def research_quick_hop() -> Hop:
    """The `research-quick` workflow: single-source quick research.

    Three-node hop:
      1. search-prep (prompt)  - SEARCH_PLAN.md (question + outline)
      2. researcher (tool/ADK) - FINDINGS.md (sections per plan)
      3. receipt (bash)       - EVAL.json + RESEARCH_RECEIPT.json

    Gate: FINDINGS.md with >= 2 sections + EVAL passed + all artifacts.
    """
    wf = Workflow(id="research-quick",
                  description="Single-source quick research (5 min)")
    search_prep = _search_prep_prompt_node()
    researcher = _researcher_adk_node()
    researcher.depends_on = ["search-prep"]
    receipt = Node(id="receipt", kind="bash", command=_receipt_command(),
                   description="Write EVAL.json + RESEARCH_RECEIPT.json")
    receipt.depends_on = ["researcher"]
    for n in (search_prep, researcher, receipt):
        wf.add_node(n)
    return Hop(
        id="research-quick", workflow=wf,
        required_artifacts=["SEARCH_PLAN.md", "FINDINGS.md", "EVAL.json",
                            "RESEARCH_RECEIPT.json"],
        gate_checks={
            "eval-json": eval_json_check(),
            "exit-codes": exit_codes_check(),
            "artifacts": required_artifact_check(
                ["SEARCH_PLAN.md", "FINDINGS.md", "EVAL.json",
                 "RESEARCH_RECEIPT.json"]
            ),
            "sections": _sections_check,
        },
        max_fix_loops=2,
    )

