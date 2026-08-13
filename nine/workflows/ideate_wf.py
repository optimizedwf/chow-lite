"""Ideate workflow - raw idea -> expand -> challenge -> refine.

The `ideate` lane of nine: expander (prompt) takes the raw idea from the
task and writes EXPANDED.md (possibilities, angles, variants); challenger
(prompt) attacks it and writes CHALLENGES.md (weaknesses, risks, killer
questions); refiner (prompt) synthesizes the survivors and writes
IDEA_BRIEF.md plus VIABILITY.json (structured score). Gate requires the
idea brief and a parseable viability JSON with a score.

Model-or-fail: without GEMINI_API_KEY the model nodes raise WorkflowError -
the job fails loud. NEVER a canned idea.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from nine.chains.chain import Hop
from nine.gates.evidence import (
    exit_codes_check,
    required_artifact_check,
)
from nine.runtime.summarizer import _gemini_generate
from nine.runtime.workflows import Node, Workflow, WorkflowError


def _require_key(lane: str) -> None:
    """Model-or-fail: every model node checks GEMINI_API_KEY first."""
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        raise WorkflowError(
            f"{lane} requires GEMINI_API_KEY - no offline fallback, "
            "nine is model-driven"
        )


def _prompt_node(node_id: str, builder) -> Node:
    """Shared prompt-node factory: build, call, write artifact, verify."""
    def _run(inputs: dict, job_dir) -> dict:
        job_dir = Path(job_dir)
        _require_key(f"ideate ({node_id})")
        prompt, target = builder(inputs, job_dir)
        text = _gemini_generate(prompt, api_key=None)
        if not (text and text.strip()):
            raise WorkflowError(
                f"ideate ({node_id}) model returned nothing - job failed "
                "loud (no offline fallback)"
            )
        (job_dir / target).write_text(text.strip(), encoding="utf-8")
        return {"output": f"wrote {target}",
                "artifact_path": str(job_dir / target)}

    return Node(id=node_id, kind="prompt", run=_run,
                max_retries=2, retry_delay_seconds=1.0,
                description=f"Prompt node writes {node_id} output "
                            "(fails loud without a model)")


def _expand_prompt_node() -> Node:
    def _build(inputs: dict, job_dir: Path):
        task = str(inputs.get("task", ""))[:600]
        prompt = (
            "You are the expander of nine, an evidence-gated agent OS. "
            "Take the raw idea below and EXPAND it. Write EXPANDED.md "
            "with:\n"
            "1. Core idea - one crisp paragraph restating the concept.\n"
            "2. Angles - 5-8 distinct directions/variants the idea could "
            "go (market, technical, audience, pricing, positioning).\n"
            "3. Adjacent opportunities - what this unlocks nearby.\n"
            "4. Quick wins - 3-5 small first steps.\n"
            "Be generative: more good options beats premature "
            "convergence.\n"
            f"Raw idea: {task}\n"
        )
        return prompt, "EXPANDED.md"
    return _prompt_node("expand", _build)


def _challenge_prompt_node() -> Node:
    def _build(inputs: dict, job_dir: Path):
        def _read(name: str, limit: int = 4000) -> str:
            p = job_dir / name
            return p.read_text(encoding="utf-8")[:limit] if p.exists() else "(missing)"

        prompt = (
            "You are the challenger of nine. Attack the expanded idea "
            "below like a skeptical investor. Write CHALLENGES.md with:\n"
            "1. Weaknesses - 5-8 numbered flaws (technical, market, "
            "execution).\n"
            "2. Risks - the top 5 risks with likelihood HIGH/MED/LOW.\n"
            "3. Killer questions - 5 questions that must be answered "
            "before building.\n"
            "4. What survives - which angles from EXPANDED.md survive "
            "the attack and why.\n"
            "Be ruthless but fair - no filler.\n\n"
            f"EXPANDED.md:\n{_read('EXPANDED.md')}\n"
        )
        return prompt, "CHALLENGES.md"
    return _prompt_node("challenge", _build)


def _refine_prompt_node() -> Node:
    def _build(inputs: dict, job_dir: Path):
        def _read(name: str, limit: int = 4000) -> str:
            p = job_dir / name
            return p.read_text(encoding="utf-8")[:limit] if p.exists() else "(missing)"

        prompt = (
            "You are the refiner of nine. Synthesize the surviving idea "
            "into a sharp brief. Write IDEA_BRIEF.md with:\n"
            "1. Pitch - one paragraph (what, who, why now).\n"
            "2. Scope - MVP in/out.\n"
            "3. Risks accepted - the top risks we proceed with anyway "
            "and mitigations.\n"
            "4. Next 3 steps.\n"
            "Then write VIABILITY.json - a JSON object with exactly:\n"
            "{\"score\": <0-100 int>, \"strengths\": [<3 strings>], "
            "\"risks\": [<3 strings>], \"verdict\": \"GO|NO-GO\"}\n"
            "Score every dimension honestly.\n\n"
            f"EXPANDED.md:\n{_read('EXPANDED.md', 3000)}\n"
            f"CHALLENGES.md:\n{_read('CHALLENGES.md')}\n"
        )
        return prompt, "IDEA_BRIEF.md"
    return _prompt_node("refine", _build)


def _viability_json_check(ctx: dict[str, Any], workdir: Path) -> tuple[bool, str]:
    """VIABILITY.json must parse and carry a numeric score + verdict."""
    p = Path(workdir) / "VIABILITY.json"
    if not p.exists():
        return False, "VIABILITY.json missing"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return False, f"VIABILITY.json is not valid JSON: {exc}"
    if not isinstance(data, dict):
        return False, "VIABILITY.json must be a JSON object"
    score = data.get("score")
    if not isinstance(score, (int, float)):
        return False, "VIABILITY.json missing numeric 'score'"
    verdict = data.get("verdict")
    if verdict not in ("GO", "NO-GO"):
        return False, "VIABILITY.json 'verdict' must be GO or NO-GO"
    return True, f"VIABILITY.json valid (score={score}, verdict={verdict})"


def ideate_hop() -> Hop:
    """The `ideate` workflow: expand -> challenge -> refine.

    Three-node hop:
      1. expand (prompt)    - EXPANDED.md (angles, variants, quick wins)
      2. challenge (prompt) - CHALLENGES.md (weaknesses, risks, survivors)
      3. refine (prompt)    - IDEA_BRIEF.md + VIABILITY.json (score+verdict)

    Gate: viability JSON valid + all artifacts + exit codes.
    """
    wf = Workflow(id="ideate",
                  description="Raw idea -> expand -> challenge -> refine")
    expander = _expand_prompt_node()
    challenger = _challenge_prompt_node()
    challenger.depends_on = ["expand"]
    refiner = _refine_prompt_node()
    refiner.depends_on = ["challenge"]
    for n in (expander, challenger, refiner):
        wf.add_node(n)
    return Hop(
        id="ideate", workflow=wf,
        required_artifacts=[
            "EXPANDED.md", "CHALLENGES.md", "IDEA_BRIEF.md", "VIABILITY.json"
        ],
        gate_checks={
            "exit-codes": exit_codes_check(),
            "artifacts": required_artifact_check(
                ["EXPANDED.md", "CHALLENGES.md", "IDEA_BRIEF.md",
                 "VIABILITY.json"]
            ),
            "viability-json": _viability_json_check,
        },
        max_fix_loops=2,
    )
