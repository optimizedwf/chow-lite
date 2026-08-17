
"""Shared prompt-budget capping for ADK instructions and task text.

torture-29 F4 / torture-35 F1: every ADK hop that embeds the task or a
fix directive into the model prompt must cap the SAME way — honor the
NINE_TASK_CAP env knob (junk values warn loudly, T24-F5/T25-F3 junk-env
convention), keep the HEAD and the TAIL (acceptance criteria usually
live at the end), and insert an explicit truncation marker so the model
knows content was cut. A silent `[:N]` tail-drop is the anti-pattern
both findings banned.
"""
from __future__ import annotations

import os as _os
import sys as _sys


def env_cap(name: str, default: int = 1400) -> int:
    """Read an int env knob with the junk-env convention: a non-numeric
    value or a value < 1 prints ONE loud stderr warning naming the
    variable and falls back to `default`."""
    raw = _os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        print(
            f"WARNING: {name}={raw!r} is not an integer - using {default}",
            file=_sys.stderr,
        )
        return default
    if value < 1:
        print(
            f"WARNING: {name}={raw!r} is < 1 - using {default}",
            file=_sys.stderr,
        )
        return default
    return value


def cap_task_text(
    text: str,
    env_name: str = "NINE_TASK_CAP",
    default: int = 1400,
    marker: str = "\n...[task truncated for model budget]...\n",
) -> str:
    """Cap a task/fix-directive string for the ADK prompt budget.

    Keeps the front (role + task) and the tail (code context /
    acceptance criteria) with an ellipsis marker in between — never a
    silent tail-drop. Text at or under the cap passes through unchanged.
    """
    limit = env_cap(env_name, default)
    if len(text) <= limit:
        return text
    head = text[: int(limit * 0.6)]
    tail = text[-(limit - int(limit * 0.6)):]
    return head + marker + tail
