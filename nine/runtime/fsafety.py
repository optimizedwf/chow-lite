"""Filesystem safety for model-controlled writes (torture T3-F7 / T5-F1).

Every ADK `write_file` FunctionTool in nine must write ONLY inside its job
dir. Before this helper, only flagship.py contained its writes; the other
~30 model-controlled write sites happily accepted `../` paths, so a model
(or a prompt-injected task) could overwrite real repo source, catalogs, or
another job's EVAL.json.

`contained_write` resolves the requested path and refuses anything that is
not strictly inside the job dir (Path.is_relative_to after resolve covers
`..` escapes, absolute paths, and symlink targets).
"""
from __future__ import annotations

from pathlib import Path


def contained_write(job_dir: Path, rel_path: str, content: str) -> str:
    """Write `content` to `rel_path` inside `job_dir` (creating parents).

    Raises ValueError when the resolved target escapes the job dir — the
    ADK FunctionTool then surfaces a tool error and the model must retry
    with a contained path (evidence gate still sees a real write or a
    failed attempt; nothing outside the job dir is ever touched).
    """
    job_dir = Path(job_dir)
    base = job_dir.resolve()
    target = (base / rel_path).resolve()
    if not target.is_relative_to(base):
        raise ValueError(
            f"refusing write outside job dir: {rel_path!r} "
            f"(resolved {target})"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"wrote {rel_path} ({len(content)} bytes)"
