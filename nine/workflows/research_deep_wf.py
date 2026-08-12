"""Research-deep workflow - iterative deep research (critique loops).

The `research-deep` lane of nine: researcher (tool/ADK) writes
DRAFT_FINDINGS.md from the source; critique (prompt) challenges it in
CRITIQUE.md (gaps, weaknesses, unverified claims); iterate (tool/ADK)
revises into ITERATED_FINDINGS.md addressing every critique point;
synthesize (prompt) merges into final FINDINGS.md with a Critique Pass
section; receipt (bash) certifies EVAL.json + RESEARCH_RECEIPT.json.
Gate requires FINDINGS.md with sections + a critique pass.

Model-or-fail: without GEMINI_API_KEY the model nodes raise WorkflowError -
the job fails loud. NEVER a canned research note.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from nine.chains.chain import Hop
from nine.gates.evidence import (
    eval_json_check,
    exit_codes_check,
    required_artifact_check,
)
from nine.runtime.summarizer import _gemini_generate
from nine.runtime.workflows import Node, Workflow, WorkflowError


def _require_key(lane: str) -> None:
    """Model-or-fail: every model node checks GEMINI_API_KEY first."""
    if not os.environ.get("GEMINI_API_KEY"):
        raise WorkflowError(
            f"{lane} requires GEMINI_API_KEY (ADK LlmAgent) - no offline "
            "fallback, nine is model-driven"
        )


def _read_workspace(job_dir: Path) -> tuple[Any, str]:
    """Return (read_fn, sources) - shared source access for model nodes."""
    def _read(name: str, limit: int = 3000) -> str:
        p = job_dir / name
        return p.read_text(encoding="utf-8")[:limit] if p.exists() else "(missing)"

    sources = ""
    if (job_dir / "solution.py").exists():
        sources = (job_dir / "solution.py").read_text(encoding="utf-8")[:4000]
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
    return _read, sources


def _researcher_adk_node() -> Node:
    """ADK LlmAgent: initial deep pass -> DRAFT_FINDINGS.md."""
    def _run(inputs: dict, job_dir) -> dict:
        from nine.runtime.adk_runtime import ADKAgentNode

        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:500]
        _require_key("research-deep (researcher)")

        from google.adk.agents import LlmAgent
        from google.adk.models import Gemini
        from google.adk.tools import FunctionTool

        def write_file(path: str, content: str) -> str:
            """Write a file into the workspace (job dir)."""
            (job_dir / path).write_text(content, encoding="utf-8")
            return f"wrote {path} ({len(content)} bytes)"

        _, sources = _read_workspace(job_dir)
        agent = LlmAgent(
            name="researcher",
            model=Gemini(model="gemini-3.6-flash"),
            instruction=(
                "You are the deep researcher of nine, an evidence-gated "
                "agent OS. Produce the first deep research pass on the "
                "single source below. Write DRAFT_FINDINGS.md with "
                "sections: Summary, Details, Evidence (file + symbol "
                "citations), Open Questions, Recommendations. Be thorough; "
                "cover behavior, edge cases, and design. Do not invent "
                "facts; cite evidence for every claim.\n"
                f"Task: {task}\n"
                f"Source:\n```python\n{sources}\n```"
            ),
            tools=[FunctionTool(write_file)],
        )
        return ADKAgentNode(agent)(inputs, job_dir)

    return Node(
        id="researcher", kind="tool", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="ADK LlmAgent writes DRAFT_FINDINGS.md (fails loud without a model)",
    )


def _critique_prompt_node() -> Node:
    """Prompt node: challenge the draft -> CRITIQUE.md."""
    def _run(inputs: dict, job_dir) -> dict:
        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:400]
        _require_key("research-deep (critique)")

        read, _ = _read_workspace(job_dir)
        prompt = (
            "You are the adversarial critic of nine, an evidence-gated "
            "agent OS. Read the draft research pass and write CRITIQUE.md "
            "with sections: Gaps (what is missing or under-covered), "
            "Weaknesses (flawed reasoning or unsupported claims), "
            "Unverified Claims (each claim lacking an Evidence citation), "
            "Suggested Improvements (numbered, actionable). Be specific - "
            "reference section/line where possible.\n"
            f"Task: {task}\n"
            f"DRAFT_FINDINGS.md:\n{read('DRAFT_FINDINGS.md')}\n"
        )
        critique = _gemini_generate(prompt, api_key=None)
        if not (critique and critique.strip()):
            raise WorkflowError(
                "research-deep (critique) model returned nothing - job "
                "failed loud (no offline fallback)"
            )
        (job_dir / "CRITIQUE.md").write_text(critique.strip(), encoding="utf-8")
        return {"output": "wrote CRITIQUE.md",
                "artifact_path": str(job_dir / "CRITIQUE.md")}

    return Node(
        id="critique", kind="prompt", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="Prompt node writes CRITIQUE.md (fails loud without a model)",
    )


def _iterate_adk_node() -> Node:
    """ADK LlmAgent: revise the draft addressing every critique point."""
    def _run(inputs: dict, job_dir) -> dict:
        from nine.runtime.adk_runtime import ADKAgentNode

        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:500]
        _require_key("research-deep (iterate)")

        from google.adk.agents import LlmAgent
        from google.adk.models import Gemini
        from google.adk.tools import FunctionTool

        def write_file(path: str, content: str) -> str:
            """Write a file into the workspace (job dir)."""
            (job_dir / path).write_text(content, encoding="utf-8")
            return f"wrote {path} ({len(content)} bytes)"

        read, sources = _read_workspace(job_dir)
        agent = LlmAgent(
            name="iterate",
            model=Gemini(model="gemini-3.6-flash"),
            instruction=(
                "You are the iteration researcher of nine. Revise the "
                "research using the critique: address EVERY point in "
                "CRITIQUE.md (fill gaps, fix weaknesses, add missing "
                "citations, apply improvements). Write ITERATED_FINDINGS.md "
                "with the same sections as the draft (Summary, Details, "
                "Evidence, Open Questions, Recommendations), plus a "
                "Changes Made section listing each critique point -> "
                "what you did. Use the write_file tool.\n"
                f"Task: {task}\n"
                f"Source:\n```python\n{sources}\n```\n"
                f"DRAFT_FINDINGS.md:\n{read('DRAFT_FINDINGS.md')}\n"
                f"CRITIQUE.md:\n{read('CRITIQUE.md')}\n"
            ),
            tools=[FunctionTool(write_file)],
        )
        return ADKAgentNode(agent)(inputs, job_dir)

    return Node(
        id="iterate", kind="tool", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="ADK LlmAgent writes ITERATED_FINDINGS.md (fails loud without a model)",
    )


def _synthesize_prompt_node() -> Node:
    """Prompt node: merge into final FINDINGS.md with a Critique Pass."""
    def _run(inputs: dict, job_dir) -> dict:
        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:400]
        _require_key("research-deep (synthesize)")

        read, _ = _read_workspace(job_dir)
        prompt = (
            "You are the synthesizer of nine. Produce the final FINDINGS.md "
            "from the iterated research, with sections: Summary, Details, "
            "Evidence, Open Questions, Recommendations, and a final "
            "Critique Pass section summarizing what the critique found and "
            "how the iteration resolved it. Keep claims evidence-cited; "
            "do not add new unverified facts.\n"
            f"Task: {task}\n"
            f"ITERATED_FINDINGS.md:\n{read('ITERATED_FINDINGS.md')}\n"
            f"CRITIQUE.md:\n{read('CRITIQUE.md', limit=2000)}\n"
        )
        final = _gemini_generate(prompt, api_key=None)
        if not (final and final.strip()):
            raise WorkflowError(
                "research-deep (synthesize) model returned nothing - job "
                "failed loud (no offline fallback)"
            )
        (job_dir / "FINDINGS.md").write_text(final.strip(), encoding="utf-8")
        return {"output": "wrote FINDINGS.md",
                "artifact_path": str(job_dir / "FINDINGS.md")}

    return Node(
        id="synthesize", kind="prompt", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="Prompt node writes FINDINGS.md (fails loud without a model)",
    )


def _receipt_command() -> str:
    """Bash node: certify deep research with EVAL.json + RESEARCH_RECEIPT.json."""
    return r"""
sections=$(grep -c '^## ' FINDINGS.md 2>/dev/null || echo 0)
if [ -f FINDINGS.md ] && [ -f CRITIQUE.md ] && [ "$sections" -ge 3 ]; then
  printf '{"checks":[{"name":"deep-findings","passed":true,"message":"%s sections + critique"}],"exit_code":0}' "$sections" > EVAL.json
else
  printf '{"checks":[{"name":"deep-findings","passed":false,"message":"FINDINGS.md/CRITIQUE.md missing or <3 sections (got %s)"}],"exit_code":1}' "$sections" > EVAL.json
fi
printf '{"draft":"DRAFT_FINDINGS.md","critique":"CRITIQUE.md","iterated":"ITERATED_FINDINGS.md","final":"FINDINGS.md","sections":%s}' "$sections" > RESEARCH_RECEIPT.json
exit 0
"""


def _findings_check(ctx: dict[str, Any], workdir: Path) -> tuple[bool, str]:
    """FINDINGS.md must exist with >= 3 sections AND mention the critique pass."""
    p = Path(workdir) / "FINDINGS.md"
    if not p.exists():
        return False, "FINDINGS.md missing"
    txt = p.read_text(encoding="utf-8")
    n = sum(1 for line in txt.splitlines() if line.startswith("## "))
    if n < 3:
        return False, f"FINDINGS.md has {n} sections (need >= 3)"
    if "Critique Pass" not in txt:
        return False, "FINDINGS.md missing Critique Pass section"
    return True, f"FINDINGS.md has {n} sections + Critique Pass"


def _critique_check(ctx: dict[str, Any], workdir: Path) -> tuple[bool, str]:
    """CRITIQUE.md must exist with at least one section header."""
    p = Path(workdir) / "CRITIQUE.md"
    if not p.exists():
        return False, "CRITIQUE.md missing"
    txt = p.read_text(encoding="utf-8")
    n = sum(1 for line in txt.splitlines() if line.startswith("## "))
    if n < 1:
        return False, "CRITIQUE.md has no sections"
    return True, f"CRITIQUE.md has {n} sections"


def research_deep_hop() -> Hop:
    """The `research-deep` workflow: iterative deep research.

    Five-node hop:
      1. researcher (tool/ADK)  - DRAFT_FINDINGS.md (initial pass)
      2. critique (prompt)      - CRITIQUE.md (gaps/weaknesses/claims)
      3. iterate (tool/ADK)     - ITERATED_FINDINGS.md (addresses critique)
      4. synthesize (prompt)    - FINDINGS.md (final + Critique Pass)
      5. receipt (bash)         - EVAL.json + RESEARCH_RECEIPT.json

    Gate: FINDINGS.md >= 3 sections + Critique Pass + CRITIQUE.md + EVAL.
    """
    wf = Workflow(id="research-deep",
                  description="Iterative deep research (critique loops)")
    researcher = _researcher_adk_node()
    critique = _critique_prompt_node()
    critique.depends_on = ["researcher"]
    iterate = _iterate_adk_node()
    iterate.depends_on = ["critique"]
    synthesize = _synthesize_prompt_node()
    synthesize.depends_on = ["iterate"]
    receipt = Node(id="receipt", kind="bash", command=_receipt_command(),
                   description="Write EVAL.json + RESEARCH_RECEIPT.json")
    receipt.depends_on = ["synthesize"]
    for n in (researcher, critique, iterate, synthesize, receipt):
        wf.add_node(n)
    return Hop(
        id="research-deep", workflow=wf,
        required_artifacts=["DRAFT_FINDINGS.md", "CRITIQUE.md",
                            "ITERATED_FINDINGS.md", "FINDINGS.md",
                            "EVAL.json", "RESEARCH_RECEIPT.json"],
        gate_checks={
            "eval-json": eval_json_check(),
            "exit-codes": exit_codes_check(),
            "artifacts": required_artifact_check(
                ["DRAFT_FINDINGS.md", "CRITIQUE.md", "ITERATED_FINDINGS.md",
                 "FINDINGS.md", "EVAL.json", "RESEARCH_RECEIPT.json"]
            ),
            "findings": _findings_check,
            "critique": _critique_check,
        },
        max_fix_loops=2,
    )
