"""Transform workflow - format conversion (CSV -> JSON etc.).

detect-format (bash) discovers the input file and writes FORMAT.md +
TARGET.txt; transform (tool/ADK) converts it per the task and writes
OUTPUT.<ext>; validate (bash) parses the output and writes EVAL.json.
Gate: EVAL passed + OUTPUT.<ext> exists/non-empty. Model-or-fail.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from nine.chains.chain import Hop
from nine.gates.evidence import (
    eval_json_check,
    exit_codes_check,
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


def _detect_command() -> str:
    """Bash: find the input file, write FORMAT.md + TARGET.txt."""
    return r"""
python - <<'PYDETECT'
import glob, json, os, sys
from pathlib import Path

cands = []
for pat in ("input.*", "data.*", "*.csv", "*.json", "*.tsv", "*.yaml", "*.yml", "*.md", "*.txt"):
    cands += glob.glob(pat)
cands = sorted(set(cands))
cands = [c for c in cands if not c.startswith(("OUTPUT.", "FORMAT.md", "TARGET.txt", "EVAL.json"))]
out = open("FORMAT.md", "w")
if not cands:
    out.write("# Format\n\nNo input file found (looked for input.*, data.*, *.csv, *.json, *.tsv, *.yaml, *.md, *.txt).\n")
    out.close()
    open("TARGET.txt", "w").write("json")
    sys.exit(0)
path = cands[0]
ext = Path(path).suffix.lstrip(".").lower() or "txt"
out.write(f"# Format\n\nSource: `{path}`\nDetected format: {ext}\n\n")
head = open(path, encoding="utf-8", errors="replace").read(1200)
out.write(f"## Head\n\n```\n{head}\n```\n")
out.close()
open("TARGET.txt", "w").write("json")  # default; transform node may override
PYDETECT
exit 0
"""


def _transform_tool_node() -> Node:
    """ADK LlmAgent: convert the input file -> OUTPUT.<ext> + TARGET.txt."""
    def _run(inputs: dict, job_dir) -> dict:
        from nine.runtime.adk_runtime import ADKAgentNode

        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:500]
        _require_key("transform (transform)")

        from google.adk.agents import LlmAgent
        from google.adk.models import Gemini
        from google.adk.tools import FunctionTool

        def write_file(path: str, content: str) -> str:
            contained_write(job_dir, path, content)
            return f"wrote {path} ({len(content)} bytes)"

        fmt = ""
        p = job_dir / "FORMAT.md"
        if p.exists():
            fmt = p.read_text(encoding="utf-8")[:2500]

        agent = LlmAgent(
            name="transformer",
            model=Gemini(model="gemini-3.6-flash"),
            instruction=(
                "You are the transformer of nine. Convert the source file "
                "described in FORMAT.md to the target format requested in "
                "the task (json, csv, yaml, tsv, md...). If the task does "
                "not name a format, default to json.\n"
                "1. First write TARGET.txt containing ONLY the output "
                "extension without a dot (e.g. `json`, `csv`, `yaml`).\n"
                "2. Then write OUTPUT.<ext> containing the FULL converted "
                "document. JSON/YAML must parse; CSV must have a header "
                "row and consistent columns. Convert every row/record - "
                "do not truncate or summarize the data.\n"
                "Read the source file from the job dir if you need more "
                "than the head shown below. Never invent values that are "
                "not in the source.\n"
                f"Task: {task}\n"
                f"FORMAT.md:\n{fmt}\n"
            ),
            tools=[FunctionTool(write_file)],
        )
        return ADKAgentNode(agent)(inputs, job_dir)

    return Node(
        id="transform", kind="tool", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="ADK LlmAgent writes OUTPUT.<ext> (fails loud without a model)",
    )


def _validate_command() -> str:
    """Bash: parse OUTPUT.<ext> per TARGET.txt, write EVAL.json."""
    return r"""
python - <<'PYVALID'
import json, os, sys
from pathlib import Path

ext = (Path("TARGET.txt").read_text().strip().lower() if Path("TARGET.txt").exists() else "json")
outp = Path(f"OUTPUT.{ext}")
ok, msg = False, f"OUTPUT.{ext} missing"
if outp.exists() and outp.stat().st_size > 0:
    try:
        if ext == "json":
            data = json.loads(outp.read_text(encoding="utf-8"))
            ok = True; msg = "valid JSON"
        elif ext in ("csv", "tsv"):
            import csv
            sep = "," if ext == "csv" else "\t"
            rows = list(csv.reader(outp.read_text(encoding="utf-8").splitlines(), delimiter=sep))
            ok = len(rows) >= 2 and len(rows[0]) >= 1; msg = f"{len(rows)} rows parsed"
        elif ext in ("yaml", "yml"):
            import yaml
            yaml.safe_load(outp.read_text(encoding="utf-8"))
            ok = True; msg = "valid YAML"
        else:
            # unstructured/unknown target = UNVERIFIABLE output: a model can
            # satisfy "non-empty file" by relabeling the format (TARGET.txt
            # is model-writable). Only parseable structured formats pass.
            ok = False; msg = f"unsupported target format '{ext}' — transform must produce parseable json/csv/tsv/yaml"
    except Exception as exc:
        ok = False; msg = f"parse error: {exc}"
else:
    msg = f"OUTPUT.{ext} missing or empty"
print(json.dumps({"checks": [{"name": "transform-validate", "passed": ok, "message": msg}], "exit_code": 0 if ok else 1}))
json.dump({"checks": [{"name": "transform-validate", "passed": ok, "message": msg}], "exit_code": 0 if ok else 1}, open("EVAL.json", "w"))
PYVALID
exit 0
"""


def _output_check(ctx: dict[str, Any], workdir: Path) -> tuple[bool, str]:
    """OUTPUT.<ext> (per TARGET.txt) must exist, be non-empty, and be a
    parseable structured format (json/csv/tsv/yaml) — a model relabeling
    TARGET.txt to a junk extension must not smuggle unverifiable output."""
    wd = Path(workdir)
    ext = "json"
    tp = wd / "TARGET.txt"
    if tp.exists():
        ext = tp.read_text(encoding="utf-8").strip().lower() or "json"
    if ext not in {"json", "csv", "tsv", "yaml", "yml"}:
        return False, (
            f"unsupported target format '{ext}' (TARGET.txt) — "
            "must be json/csv/tsv/yaml"
        )
    outp = wd / f"OUTPUT.{ext}"
    if not outp.exists():
        return False, f"OUTPUT.{ext} missing"
    size = outp.stat().st_size
    if size < 10:
        return False, f"OUTPUT.{ext} too small ({size} bytes)"
    return True, f"OUTPUT.{ext} present ({size} bytes)"


def transform_hop() -> Hop:
    """The `transform` workflow: format conversion.

    1. detect-format (bash) - FORMAT.md + TARGET.txt
    2. transform (tool/ADK) - OUTPUT.<ext> (write_file)
    3. validate (bash)      - parse check -> EVAL.json

    Gate: EVAL passed + OUTPUT.<ext> non-empty + artifacts + exits.
    """
    wf = Workflow(id="transform",
                  description="Format conversion (CSV -> JSON etc.)")
    detect = Node(id="detect-format", kind="bash", command=_detect_command(),
                  description="Detect input format -> FORMAT.md + TARGET.txt")
    transform = _transform_tool_node()
    transform.depends_on = ["detect-format"]
    validate = Node(id="validate", kind="bash", command=_validate_command(),
                    description="Parse OUTPUT.<ext>, write EVAL.json")
    validate.depends_on = ["transform"]
    for n in (detect, transform, validate):
        wf.add_node(n)
    return Hop(
        id="transform", workflow=wf,
        required_artifacts=["FORMAT.md", "TARGET.txt", "EVAL.json"],
        gate_checks={
            "eval-json": eval_json_check(),
            "exit-codes": exit_codes_check(),
            "artifacts": required_artifact_check(
                ["FORMAT.md", "TARGET.txt", "EVAL.json"]
            ),
            "output": _output_check,
        },
        max_fix_loops=2,
    )
