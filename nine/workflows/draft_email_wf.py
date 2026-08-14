"""Draft-email workflow - tone-aware reply/compose -> DRAFT.md.

The `draft-email` lane of nine: drafter (prompt) writes a first-pass email
DRAFT.md from the task (recipient, context, tone spec); reviewtone
(prompt) checks the draft against the requested tone and writes
TONE_REVIEW.md (tone verdict + adjustments); reviser (prompt) applies the
adjustments, overwrites DRAFT.md (final) and appends TONE_REVISION.md
entries. Gate requires a non-trivial DRAFT.md and a tone verdict.

Model-or-fail: without GEMINI_API_KEY the model nodes raise WorkflowError -
the job fails loud. NEVER a canned email.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nine.chains.chain import Hop
from nine.gates.evidence import (
    exit_codes_check,
    file_nonempty_check,
    required_artifact_check,
)
from nine.runtime.llm_provider import key_available
from nine.runtime.summarizer import _gemini_generate
from nine.runtime.workflows import Node, Workflow, WorkflowError


def _require_key(lane: str) -> None:
    """Model-or-fail: every model node checks GEMINI_API_KEY first."""
    if not key_available():
        raise WorkflowError(
            f"{lane} requires an LLM key (gemini: GEMINI_API_KEY; openai: NINE_LLM_API_KEY/OPENCODE_GO_API_KEY) - no offline fallback, "
            "nine is model-driven"
        )


def _prompt_node(node_id: str, builder) -> Node:
    """Shared prompt-node factory: build, call, write artifact, verify."""
    def _run(inputs: dict, job_dir) -> dict:
        job_dir = Path(job_dir)
        _require_key(f"draft-email ({node_id})")
        prompt, target = builder(inputs, job_dir)
        text = _gemini_generate(prompt, api_key=None)
        if not (text and text.strip()):
            raise WorkflowError(
                f"draft-email ({node_id}) model returned nothing - job "
                "failed loud (no offline fallback)"
            )
        (job_dir / target).write_text(text.strip(), encoding="utf-8")
        return {"output": f"wrote {target}",
                "artifact_path": str(job_dir / target)}

    return Node(id=node_id, kind="prompt", run=_run,
                max_retries=2, retry_delay_seconds=1.0,
                description=f"Prompt node writes {node_id} output "
                            "(fails loud without a model)")


def _draft_prompt_node() -> Node:
    def _build(inputs: dict, job_dir: Path):
        task = str(inputs.get("task", ""))[:600]
        prompt = (
            "You are the email drafter of nine, an evidence-gated agent "
            "OS. Compose a first-pass email DRAFT.md from the task "
            "below. Write:\n"
            "- `Subject:` line, then a blank line, then the body.\n"
            "- Follow the recipient, context, and tone spec in the task "
            "exactly; if no tone is given, use professional-and-warm.\n"
            "- Be specific and human; avoid boilerplate. Mark anything "
            "unknown as [TBD] instead of inventing it.\n"
            f"Task: {task}\n"
        )
        return prompt, "DRAFT.md"
    return _prompt_node("draft", _build)


def _reviewtone_prompt_node() -> Node:
    def _build(inputs: dict, job_dir: Path):
        task = str(inputs.get("task", ""))[:400]
        draft = ""
        p = job_dir / "DRAFT.md"
        if p.exists():
            draft = p.read_text(encoding="utf-8")[:4000]
        prompt = (
            "You are the tone reviewer of nine. Check the email draft "
            "against the task's tone spec. Write TONE_REVIEW.md with:\n"
            "1. `Tone: APPROVE` or `Tone: REVISE` on its own line.\n"
            "2. Tone check - 2-3 lines: does the draft match the "
            "requested tone? Quote the tone spec.\n"
            "3. Adjustments - numbered list of concrete rewrites "
            "(specific phrasing fixes, not general advice).\n"
            "4. Length and clarity notes.\n"
            f"Task tone spec: {task}\n"
            f"DRAFT.md:\n{draft}\n"
        )
        return prompt, "TONE_REVIEW.md"
    return _prompt_node("reviewtone", _build)


def _revise_prompt_node() -> Node:
    def _build(inputs: dict, job_dir: Path):
        def _read(name: str, limit: int = 4000) -> str:
            p = job_dir / name
            return p.read_text(encoding="utf-8")[:limit] if p.exists() else "(missing)"

        prompt = (
            "You are the email reviser of nine. Apply every adjustment "
            "in TONE_REVIEW.md to the email DRAFT.md.\n"
            "1. Overwrite DRAFT.md with the final email (Subject line + "
            "body) that satisfies the tone verdict.\n"
            "2. Append to TONE_REVISION.md (create if missing) one line "
            "per applied adjustment:\n"
            "   `- [x] Adjustment N: <issue> -> <what you changed>`\n"
            f"DRAFT.md:\n{_read('DRAFT.md')}\n"
            f"TONE_REVIEW.md:\n{_read('TONE_REVIEW.md', 3000)}\n"
        )
        return prompt, "TONE_REVISION.md"
    return _prompt_node("revise", _build)


def _tone_review_check(ctx: dict[str, Any], workdir: Path) -> tuple[bool, str]:
    """TONE_REVIEW.md must carry a `Tone:` verdict line."""
    p = Path(workdir) / "TONE_REVIEW.md"
    if not p.exists():
        return False, "TONE_REVIEW.md missing"
    txt = p.read_text(encoding="utf-8")
    if "Tone:" not in txt:
        return False, "TONE_REVIEW.md missing Tone verdict line"
    return True, "TONE_REVIEW.md has a tone verdict"


_tone_review_check.expected = ["TONE_REVIEW.md"]  # type: ignore[attr-defined]  # torture-17 F2 tag
def draft_email_hop() -> Hop:
    """The `draft-email` workflow: tone-aware draft -> reviewtone -> revise.

    Three-node hop:
      1. draft (prompt)     - DRAFT.md v1 (subject + body)
      2. reviewtone (prompt) - TONE_REVIEW.md (Tone verdict + adjustments)
      3. revise (prompt)    - final DRAFT.md + TONE_REVISION.md

    Gate: non-empty DRAFT.md + tone verdict + artifacts + exit codes.
    """
    wf = Workflow(id="draft-email",
                  description="Tone-aware email compose/reply -> DRAFT.md")
    drafter = _draft_prompt_node()
    reviewer = _reviewtone_prompt_node()
    reviewer.depends_on = ["draft"]
    reviser = _revise_prompt_node()
    reviser.depends_on = ["reviewtone"]
    for n in (drafter, reviewer, reviser):
        wf.add_node(n)
    return Hop(
        id="draft-email", workflow=wf,
        required_artifacts=["DRAFT.md", "TONE_REVIEW.md", "TONE_REVISION.md"],
        gate_checks={
            "exit-codes": exit_codes_check(),
            "artifacts": required_artifact_check(
                ["DRAFT.md", "TONE_REVIEW.md", "TONE_REVISION.md"]
            ),
            "nonempty": file_nonempty_check("DRAFT.md", min_chars=40),
            "revision-log": file_nonempty_check("TONE_REVISION.md", min_chars=10),
            "tone-verdict": _tone_review_check,
        },
        max_fix_loops=2,
    )
