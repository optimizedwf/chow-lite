"""Summarizer — the semantic-context step between hop handoffs.

Cerebras lesson: "the line cook doesn't get the 15,000-token master plan...
it gets the minimum viable context to cook one specific dish." The research
hop's raw findings are distilled here into a bounded HANDOFF.md that the
plan hop reads instead of the full document.

Degrades to a deterministic extractive summary with no GEMINI_API_KEY, so
the core loop and CI stay hermetic (same doctrine as gemma.py / adk_runtime).
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from nine.runtime.workflows import Node, WorkflowError

DEFAULT_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
MAX_SOURCE_CHARS = 12_000  # cap the LLM input; full doc stays on disk


def _extractive(text: str, max_words: int) -> str:
    """Deterministic fallback: bounded head summary (offline/CI path)."""
    words = re.split(r"\s+", text.strip())
    if not words:
        return ""
    if len(words) <= max_words:
        return text.strip()
    head = " ".join(words[:max_words])
    return head + " \u2026 [extractive summary \u2014 set GEMINI_API_KEY for semantic distillation]"


def _gemini_generate(prompt: str, api_key: str | None, timeout: int = 90) -> str | None:
    """Semantic distillation via Gemini (google-genai). None on any failure."""
    key = api_key or os.environ.get("GEMINI_API_KEY")
    if not key:
        return None
    try:
        from google import genai

        client = genai.Client(api_key=key)
        resp = client.models.generate_content(model=DEFAULT_MODEL, contents=prompt)
        return resp.text if resp.text else None
    except Exception:  # noqa: BLE001 - degrade to extractive on any API failure
        return None


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
    if model_text and model_text.strip():
        return model_text.strip(), DEFAULT_MODEL
    return _extractive(text, max_words), "deterministic-extractive"


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
