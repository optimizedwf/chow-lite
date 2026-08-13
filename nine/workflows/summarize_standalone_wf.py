"""Summarize-standalone workflow - one-source distillation -> SUMMARY.md.

The `summarize-standalone` lane of nine: read-source (bash) collects the
workspace source into SOURCE.md; the summarizer node (summarize kind,
promoted from the runtime summarizer used by the flagship chain) distills
it into a bounded SUMMARY.md. Gate requires a non-empty SUMMARY.md.

Model-or-fail: without GEMINI_API_KEY the summarizer raises WorkflowError -
the job fails loud. NEVER a canned summary.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from nine.chains.chain import Hop
from nine.gates.evidence import (
    CheckFn,
    exit_codes_check,
    file_nonempty_check,
    required_artifact_check,
)
from nine.runtime.summarizer import build_summarize_node
from nine.runtime.workflows import Node, Workflow


def _read_source_command() -> str:
    """Bash node: collect the workspace source into SOURCE.md.

    Reads solution.py (or the solution/ tree) into SOURCE.md with a file
    inventory header. Always exits 0; evidence is the artifact.
    """
    return r"""
echo '# Source (for summarization)' > SOURCE.md
echo '## Inventory' >> SOURCE.md
if [ -f solution.py ]; then
  echo '- solution.py (top-level module)' >> SOURCE.md
  echo '## solution.py' >> SOURCE.md
  cat solution.py >> SOURCE.md
elif [ -d solution ]; then
  find solution -name '*.py' -not -path '*/__pycache__/*' | sort | while read -r f; do
    echo "- $f" >> SOURCE.md
  done
  echo '## Sources' >> SOURCE.md
  for f in $(find solution -name '*.py' -not -path '*/__pycache__/*' | sort); do
    echo "### $f" >> SOURCE.md
    cat "$f" >> SOURCE.md
  done
else
  echo '- (no source files found)' >> SOURCE.md
fi
exit 0
"""


def _summarizer_node() -> Node:
    """Summarize-kind node: distill SOURCE.md -> SUMMARY.md (model-driven)."""
    return build_summarize_node(
        source="SOURCE.md",
        target="SUMMARY.md",
        max_words=200,
        header="# Summary",
    )


def _source_present_check() -> CheckFn:
    """Gate: SOURCE.md must contain at least one real source inventory line.

    A "summary of nothing" (empty workspace, only the '(no source files
    found)' marker in SOURCE.md) must NEVER SHIP — the lane's whole purpose
    is to distill real source (torture finding T2-F8).
    """

    def _check(ctx: dict[str, Any], workdir: Path) -> tuple[bool, str]:
        src = Path(workdir) / "SOURCE.md"
        if not src.exists():
            return False, "SOURCE.md missing — nothing was collected"
        text = src.read_text(encoding="utf-8", errors="replace")
        if "- (no source files found)" in text:
            return False, "no source files in workspace — nothing to summarize"
        return True, "source inventory present"

    return _check


def summarize_standalone_hop() -> Hop:
    """The `summarize-standalone` workflow: one-source distillation.

    Two-node hop:
      1. read-source (bash)  - SOURCE.md (workspace source)
      2. summarizer (summarize) - SUMMARY.md (distilled, model-driven)

    Gate: SUMMARY.md non-empty + both artifacts + exit codes.
    """
    wf = Workflow(id="summarize-standalone",
                  description="One-source distillation -> SUMMARY.md")
    read_source = Node(id="read-source", kind="bash",
                       command=_read_source_command(),
                       description="Collect source into SOURCE.md")
    summarizer = _summarizer_node()
    summarizer.depends_on = ["read-source"]
    wf.add_node(read_source)
    wf.add_node(summarizer)
    return Hop(
        id="summarize-standalone", workflow=wf,
        required_artifacts=["SOURCE.md", "SUMMARY.md"],
        gate_checks={
            "exit-codes": exit_codes_check(),
            "artifacts": required_artifact_check(["SOURCE.md", "SUMMARY.md"]),
            "nonempty": file_nonempty_check("SUMMARY.md", min_chars=20),
            "source-present": _source_present_check(),
        },
        max_fix_loops=2,
    )
