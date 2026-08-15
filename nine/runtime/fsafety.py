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
    # ADK models routinely emit "/workspace/ROOT_CAUSE.md" (the InMemory
    # session's virtual workspace root) instead of a bare relative path —
    # slice-44: qwen3:8b wrote "/workspace/ROOT_CAUSE.md", contained_write
    # refused it, and the tool error made the model bail (nondeterministic
    # "empty stream"). Map an absolute /workspace (or /workspace/...) path
    # onto the job dir so the write lands inside the sandbox as intended.
    if rel_path.startswith("/workspace"):
        rel_path = rel_path[len("/workspace"):].lstrip("/")
    target = (base / rel_path).resolve()
    if not target.is_relative_to(base):
        raise ValueError(
            f"refusing write outside job dir: {rel_path!r} "
            f"(resolved {target})"
        )
    # torture-24 F3: write-side FIFO guard. write_text() opens O_WRONLY, so
    # a pre-existing FIFO/device/socket at a predictable target
    # (solution.py / EVAL.json / HANDOFF.md) blocks until the node timeout
    # (the bench seeds test_solution.py and every flagship hop writes
    # EVAL.json at known names). is_file() is False for non-regular files,
    # so refuse loudly and let the tool error surface to the model instead
    # of hanging the node. (T21-F1 guarded the READ side; this is the same
    # family on the WRITE path, which the read guards never touch.)
    if target.exists() and not target.is_file():
        kind = "directory" if target.is_dir() else "non-regular file"
        raise ValueError(
            f"refusing write to {kind}: {rel_path!r} "
            f"(resolved {target})"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"wrote {rel_path} ({len(content)} bytes)"
