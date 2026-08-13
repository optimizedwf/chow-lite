"""Pipeline workflow - multi-stage ETL (read -> transform -> load -> validate).

read (bash) inventories the inputs into SOURCE.md + INPUT_FILES.txt;
transform (tool/ADK) maps/cleans records per the task into STAGE.json;
load (bash) materializes the final OUTPUT.json (JSONL-ready array of
records) + LOAD.md; validate (bash) parses OUTPUT.json and writes
EVAL.json. Gate: EVAL passed + OUTPUT.json non-empty + artifacts.
Model-or-fail: no key -> WorkflowError at the transform node.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from nine.chains.chain import Hop
from nine.gates.evidence import (
    eval_json_check,
    exit_codes_check,
    file_nonempty_check,
    required_artifact_check,
)
from nine.runtime.fsafety import contained_write
from nine.runtime.workflows import Node, Workflow, WorkflowError


def _require_key(lane: str) -> None:
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        raise WorkflowError(
            f"{lane} requires GEMINI_API_KEY (ADK LlmAgent) - no offline "
            "fallback, nine is model-driven"
        )


def _read_command() -> str:
    """Bash: inventory + concatenate inputs -> SOURCE.md + INPUT_FILES.txt."""
    return r"""
python - <<'PYREAD'
import glob, sys
from pathlib import Path

cands = []
for pat in ("input.*", "data.*", "*.csv", "*.json", "*.jsonl", "*.tsv", "*.txt"):
    cands += glob.glob(pat)
cands = sorted(set(cands))
cands = [c for c in cands if not c.startswith(("STAGE.", "OUTPUT.", "SOURCE.md", "INPUT_FILES.txt", "EVAL.json", "LOAD.md"))]
open("INPUT_FILES.txt", "w").write("\n".join(cands) + "\n")
out = open("SOURCE.md", "w")
out.write(f"# Source\n\nInputs ({len(cands)}):\n")
for c in cands:
    out.write(f"- `{c}`\n")
for c in cands[:6]:
    out.write(f"\n## {c}\n\n```\n")
    out.write(Path(c).read_text(encoding="utf-8", errors="replace")[:2500])
    out.write("\n```\n")
out.close()
PYREAD
exit 0
"""


def _transform_tool_node() -> Node:
    """ADK LlmAgent: map/clean records -> STAGE.json."""
    def _run(inputs: dict, job_dir) -> dict:
        from nine.runtime.adk_runtime import ADKAgentNode

        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:500]
        _require_key("pipeline (transform)")

        from google.adk.agents import LlmAgent
        from google.adk.models import Gemini
        from google.adk.tools import FunctionTool

        def write_file(path: str, content: str) -> str:
            contained_write(job_dir, path, content)
            return f"wrote {path} ({len(content)} bytes)"

        src = ""
        p = job_dir / "SOURCE.md"
        if p.exists():
            src = p.read_text(encoding="utf-8")[:3500]

        agent = LlmAgent(
            name="pipeline-transform",
            model=Gemini(model="gemini-3.6-flash"),
            instruction=(
                "You are the transform stage of a nine ETL pipeline. "
                "Apply the task's mapping/cleaning rules to every record "
                "in SOURCE.md and write STAGE.json: a JSON array of "
                "records (objects). Rules:\n"
                "- Every source record maps to exactly one output record "
                "(no dropping rows unless the task says so).\n"
                "- Normalize field names to snake_case; keep types "
                "consistent (numbers as numbers, not strings).\n"
                "- Never invent values - missing fields become null or "
                "are omitted.\n"
                "- STAGE.json must be valid JSON that json.loads.\n"
                f"Task: {task}\n"
                f"SOURCE.md:\n{src}\n"
            ),
            tools=[FunctionTool(write_file)],
        )
        return ADKAgentNode(agent)(inputs, job_dir)

    return Node(
        id="transform", kind="tool", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="ADK LlmAgent writes STAGE.json (fails loud without a model)",
    )


def _load_command() -> str:
    """Bash: materialize OUTPUT.json + LOAD.md from STAGE.json."""
    return r"""
python - <<'PYLOAD'
import json, sys
from pathlib import Path

if not Path("STAGE.json").exists():
    json.dump({"records": [], "loaded": 0}, open("OUTPUT.json", "w"))
    open("LOAD.md", "w").write("# Load\n\nSTAGE.json missing - loaded 0 records.\n")
    sys.exit(0)
try:
    data = json.loads(Path("STAGE.json").read_text(encoding="utf-8"))
    if not isinstance(data, list):
        data = [data]
except Exception as exc:
    json.dump({"records": [], "loaded": 0, "error": str(exc)}, open("OUTPUT.json", "w"))
    open("LOAD.md", "w").write(f"# Load\n\nSTAGE.json parse error: {exc}\n")
    sys.exit(0)
json.dump({"records": data, "loaded": len(data)}, open("OUTPUT.json", "w"), indent=2)
open("LOAD.md", "w").write(f"# Load\n\nLoaded {len(data)} records into OUTPUT.json.\n")
PYLOAD
exit 0
"""


def _validate_command() -> str:
    """Bash: check OUTPUT.json, write EVAL.json."""
    return r"""
python - <<'PYVALID'
import json, sys
from pathlib import Path

ok, msg = False, "OUTPUT.json missing"
if Path("OUTPUT.json").exists():
    try:
        data = json.loads(Path("OUTPUT.json").read_text(encoding="utf-8"))
        recs = data.get("records") if isinstance(data, dict) else data
        if isinstance(recs, list) and len(recs) > 0:
            ok, msg = True, f"{len(recs)} records loaded and valid"
        else:
            msg = "OUTPUT.json has no records"
    except Exception as exc:
        msg = f"OUTPUT.json parse error: {exc}"
json.dump({"checks": [{"name": "pipeline-validate", "passed": ok, "message": msg}], "exit_code": 0 if ok else 1}, open("EVAL.json", "w"))
PYVALID
exit 0
"""


def _pipeline_output_check(ctx: dict[str, Any], workdir: Path) -> tuple[bool, str]:
    """OUTPUT.json must parse and contain >= 1 record."""
    wd = Path(workdir)
    p = wd / "OUTPUT.json"
    if not p.exists():
        return False, "OUTPUT.json missing"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        recs = data.get("records") if isinstance(data, dict) else data
        if isinstance(recs, list) and len(recs) > 0:
            return True, f"{len(recs)} records"
        return False, "OUTPUT.json has no records"
    except (json.JSONDecodeError, OSError, TypeError) as exc:
        return False, f"OUTPUT.json invalid: {exc}"


def pipeline_hop() -> Hop:
    """The `pipeline` workflow: multi-stage ETL.

    1. read (bash)      - SOURCE.md + INPUT_FILES.txt
    2. transform (tool) - STAGE.json (ADK LlmAgent, model-or-fail)
    3. load (bash)      - OUTPUT.json + LOAD.md
    4. validate (bash)  - EVAL.json

    Gate: EVAL passed + OUTPUT.json with >=1 record + artifacts + exits.
    """
    wf = Workflow(id="pipeline",
                  description="Multi-stage ETL: read -> transform -> load -> validate")
    read = Node(id="read", kind="bash", command=_read_command(),
                description="Inventory inputs -> SOURCE.md + INPUT_FILES.txt")
    transform = _transform_tool_node()
    transform.depends_on = ["read"]
    load = Node(id="load", kind="bash", command=_load_command(),
                description="Materialize OUTPUT.json + LOAD.md")
    load.depends_on = ["transform"]
    validate = Node(id="validate", kind="bash", command=_validate_command(),
                    description="Check OUTPUT.json, write EVAL.json")
    validate.depends_on = ["load"]
    for n in (read, transform, load, validate):
        wf.add_node(n)
    return Hop(
        id="pipeline", workflow=wf,
        required_artifacts=["SOURCE.md", "INPUT_FILES.txt", "STAGE.json",
                            "OUTPUT.json", "LOAD.md", "EVAL.json"],
        gate_checks={
            "eval-json": eval_json_check(),
            "exit-codes": exit_codes_check(),
            "artifacts": required_artifact_check(
                ["SOURCE.md", "INPUT_FILES.txt", "STAGE.json",
                 "OUTPUT.json", "LOAD.md", "EVAL.json"]
            ),
            "output-json": _pipeline_output_check,
            "load-md": file_nonempty_check("LOAD.md", min_chars=10),
        },
        max_fix_loops=2,
    )
