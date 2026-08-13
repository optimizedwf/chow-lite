"""Respond workflow — the universal lane of the nine loop.

Doctrine: EVERY prompt goes through a workflow. A task that matches no
specialist lane still becomes a first-class job: it is routed to `respond`,
executed (the model writes RESPONSE.md), verified (artifact present +
non-trivial), and either SHIPs or is marked BLOCKed. There is no "direct
answer" escape hatch: nothing leaves the system unverified, so the
ROUTE->EXECUTE->VERIFY->LEARN loop covers 100% of traffic, not just the
lanes a router happens to recognize.

Model-or-fail: nine is model-driven. Without GEMINI_API_KEY (or when the
model returns nothing usable) this node raises WorkflowError — the job
fails loud. There is NO offline/deterministic answer: a canned reply would
be fabricated output, which this project never produces.

This is also the "decide what to respond with" node: the model gets the
task plus the memory graph's matching prior summaries (when available),
so even a casual greeting is answered with retrieved context, not a blank
chat.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nine.gates.evidence import EvidenceGate, file_nonempty_check, required_artifact_check
from nine.runtime.workflows import Node, Workflow

DEFAULT_MODEL = "gemini-3.6-flash"


def respond_text(task: str, max_chars: int = 600) -> tuple[str, str]:
    """Model-generated answer. NEVER an offline/deterministic fallback.

    Returns (text, model_used). Raises WorkflowError when GEMINI_API_KEY is
    missing or the model returns no usable text — the job then fails loud
    instead of fabricating a canned reply.
    """
    import os

    from nine.runtime.workflows import WorkflowError

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        raise WorkflowError(
            "respond requires GEMINI_API_KEY — no offline fallback "
            "(nine is model-driven)"
        )
    from google import genai

    client = genai.Client(api_key=key)
    resp = client.models.generate_content(
        model=os.environ.get("GEMINI_MODEL", DEFAULT_MODEL),
        contents=(
            "You are the respond workflow of an evidence-gated agent "
            "operating system. Write a direct, useful answer to the "
            f"task below (<= {max_chars} chars).\n\nTask: {task[:2000]}"
        ),
    )
    if not (resp.text and resp.text.strip()):
        raise WorkflowError(
            "respond: model returned no usable text — job failed loud "
            "(no offline fallback)"
        )
    return resp.text.strip()[:max_chars], "gemini"


def _respond_run(inputs: dict[str, Any], job_dir: Path) -> dict[str, Any]:
    """Node body: write RESPONSE.md into the job dir (auto-artifact)."""
    task = str(inputs.get("task", ""))
    text, model = respond_text(task)
    target = Path(job_dir) / "RESPONSE.md"
    target.write_text(text + "\n", encoding="utf-8")
    return {"output": text, "artifact": str(target), "model_used": model}


def respond_workflow() -> Workflow:
    """The `respond` workflow: one prompt node -> RESPONSE.md."""
    wf = Workflow(id="respond", description="Answer a general task directly")
    wf.add_node(Node(
        id="respond", kind="prompt", run=_respond_run, max_retries=2,
        retry_delay_seconds=1.0,
        description="write RESPONSE.md (Gemini; fails loud without a model)",
    ))
    return wf


def respond_gate() -> EvidenceGate:
    """Gate for the respond workflow: artifact present + non-trivial text."""
    gate = EvidenceGate()
    gate.register_check("response-md", required_artifact_check(["RESPONSE.md"]))
    gate.register_check("response-nonempty", file_nonempty_check("RESPONSE.md", min_chars=10))
    return gate
