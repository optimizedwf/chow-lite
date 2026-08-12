"""Summarizer — the semantic-context step between hop handoffs.

Cerebras lesson: "the line cook doesn't get the 15,000-token master plan...
it gets the minimum viable context to cook one specific dish." The research
hop's raw findings are distilled here into a bounded HANDOFF.md that the
plan hop reads instead of the full document.

Model-or-fail: nine is model-driven. Distillation requires Gemini; with no
GEMINI_API_KEY (or an API failure) summarize_text raises WorkflowError and
the job fails loud. There is NO extractive/offline fallback — a mechanical
head-copy would be fabricated output.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from nine.runtime.workflows import Node, WorkflowError

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
MAX_SOURCE_CHARS = 12_000  # cap the LLM input; full doc stays on disk


def _gemini_generate(prompt: str, api_key: str | None, timeout: int = 90) -> str:
    """Semantic distillation via Gemini (google-genai).

    Model-or-fail: missing key or API failure raises WorkflowError — the
    summarizer never falls back to fabricated output.
    """
    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        raise WorkflowError(
            "summarize requires GEMINI_API_KEY — no offline fallback "
            "(nine is model-driven)"
        )
    from google import genai

    client = genai.Client(api_key=key)
    resp = client.models.generate_content(model=DEFAULT_MODEL, contents=prompt)
    if not (resp.text and resp.text.strip()):
        raise WorkflowError(
            "summarize: model returned no usable text — job failed loud "
            "(no offline fallback)"
        )
    return resp.text.strip()


def summarize_text(
    text: str,
    max_words: int = 120,
    task: str = "",
    api_key: str | None = None,
) -> tuple[str, str]:
    """Distill text to <=max_words. Returns (summary, model_used)."""
    if not text.strip():
        return "", "empty"
    prompt = (
        "You are the summarizer hop of nine, an evidence-gated agent OS.\n"
        "Compress the research notes below into a handoff brief of at most "
        f"{max_words} words for the next agent. Keep: what was found, key "
        "facts/numbers, open risks, and what to do next. No filler, no "
        "markdown headers.\n"
        f"\nTask: {task[:500]}\n\nResearch notes:\n{text[:MAX_SOURCE_CHARS]}"
    )
    model_text = _gemini_generate(prompt, api_key=api_key)
    return model_text, DEFAULT_MODEL


def build_summarize_node(
    source: str,
    target: str = "HANDOFF.md",
    max_words: int = 120,
    depends_on: list[str] | None = None,
) -> Node:
    """A `summarize` node: distill ``source`` artifact -> ``target`` file.

    The target file is written into the job dir, so it auto-registers as an
    artifact in the evidence manifest (and, on chain SHIP, is what the
    MemoryGraph records as the hop's semantic summary).
    """
    depends = depends_on or [Path(source).stem]

    def _run(inputs: dict[str, Any], job_dir: Path) -> dict[str, Any]:
        job_dir = Path(job_dir)
        src = job_dir / source
        if not src.exists():
            raise WorkflowError(
                f"summarize node: source '{source}' not found in job dir "
                f"(missing upstream node?)"
            )
        raw = src.read_text(encoding="utf-8", errors="replace")
        summary, model_used = summarize_text(
            raw, max_words=max_words, task=str(inputs.get("task", ""))
        )
        body = "# Handoff summary (distilled)\n\n" + summary + "\n"
        (job_dir / target).write_text(body, encoding="utf-8")
        return {
            "summary": summary,
            "model": model_used,
            "path": str(job_dir / target),
            "chars_in": len(raw),
            "chars_out": len(summary),
        }

    return Node(
        id=f"summarize-{Path(source).stem}",
        kind="summarize",
        run=_run,
        depends_on=depends,
        description=f"Distill {source} -> {target} (minimum viable context)",
    )
