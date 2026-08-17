
"""Round-18 torture harvest tests (torture-35).

T35-F1 (MED): debug_wf diagnose/patch hops used silent head-only
`[:N]` slices for the task and a hardcoded `[:1500]` for fix_directive
— the exact anti-pattern torture-29 F4 banned (tail where acceptance
criteria live is dropped with NO marker, and NINE_TASK_CAP is
ignored). Both sites now route through the shared
nine/runtime/truncate.cap_task_text (head+tail with ellipsis marker,
NINE_TASK_CAP honored, junk env warns loudly).
"""
from __future__ import annotations

import inspect

from nine.runtime.truncate import cap_task_text


# ---------------------------------------------------------------- T35-F1 ---
def test_t35_f1_debug_wf_uses_shared_marker_cap():
    """The debug workflow's diagnose + patch nodes must cap the task and
    fix_directive through cap_task_text — never a silent `[:N]` tail-drop
    or a hardcoded `[:1500]` that ignores NINE_TASK_CAP."""
    import nine.workflows.debug_wf as dbg

    src = inspect.getsource(dbg)
    # the shared capper is imported and used
    assert "from nine.runtime.truncate import cap_task_text" in src
    assert src.count("task = cap_task_text(") == 2, (
        "diagnose + patch nodes must both cap the task")
    assert src.count("fix_dir = cap_task_text(") == 2, (
        "diagnose + patch nodes must both cap fix_directive")
    # the banned patterns are gone from executable code
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"""'):
            continue
        assert "[:_task_cap]" not in line, f"silent slice remains: {line}"
        assert "[:1500]" not in line, f"hardcoded 1500 remains: {line}"


# ---------------------------------------------------------------- T35-F1 ---
def test_t35_f1_cap_task_text_keeps_tail_and_marks():
    """Behavioral pin of the shared capper: tail (where acceptance
    criteria live) survives, an explicit marker signals the cut, short
    text passes through unchanged, junk env warns and falls back."""
    import sys

    long = ("HEAD " * 300) + "ACCEPT_CRITERIA_TAIL_XYZ"
    # direct call with explicit cap: tail must survive
    out = cap_task_text(long, env_name="NINE_TASK_CAP_TEST", default=600)
    assert "ACCEPT_CRITERIA_TAIL_XYZ" in out, "tail must survive the cap"
    assert "[task truncated" in out, "marker must signal the cut"
    assert "HEAD" in out[:100], "head must survive"
    assert len(out) <= 600 + 50

    assert cap_task_text("short", env_name="NINE_TASK_CAP_TEST",
                         default=600) == "short"

    # junk env: loud warning + default fallback (T24-F5 convention)
    import io
    import os
    os.environ["NINE_TASK_CAP_TEST"] = "2k"
    buf = io.StringIO()
    old = sys.stderr
    sys.stderr = buf
    try:
        out2 = cap_task_text(long, env_name="NINE_TASK_CAP_TEST",
                             default=1400)
    finally:
        sys.stderr = old
        os.environ.pop("NINE_TASK_CAP_TEST", None)
    assert "WARNING: NINE_TASK_CAP_TEST" in buf.getvalue(), "junk env warns"
    assert len(out2) <= 1400 + 50, "falls back to default cap"
