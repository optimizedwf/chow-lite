"""Analyze workflow - dataset -> explore -> insights (INSIGHTS.md + chart.png).

The `analyze` lane of nine: inspect (bash: pandas) profiles the dataset
into DATA_PROFILE.md; explore (tool/ADK) reads the profile + data and
writes EXPLORATION.md (patterns, hypotheses); visualize (bash:
matplotlib, Agg backend) renders chart.png from the data; report (prompt)
synthesizes INSIGHTS.md. Gate requires INSIGHTS.md and a non-trivial
chart.png.

Model-or-fail: without GEMINI_API_KEY the model nodes raise WorkflowError -
the job fails loud. NEVER a canned analysis.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from nine.chains.chain import Hop
from nine.gates.evidence import (
    exit_codes_check,
    file_nonempty_check,
    required_artifact_check,
)
from nine.runtime.summarizer import _gemini_generate
from nine.runtime.workflows import Node, Workflow, WorkflowError


def _require_key(lane: str) -> None:
    """Model-or-fail: every model node checks GEMINI_API_KEY first."""
    if not os.environ.get("GEMINI_API_KEY"):
        raise WorkflowError(
            f"{lane} requires GEMINI_API_KEY (ADK LlmAgent) - no offline "
            "fallback, nine is model-driven"
        )


def _inspect_command() -> str:
    """Bash (pandas): profile the dataset -> DATA_PROFILE.md."""
    return r"""
export MPLBACKEND=Agg
python - <<'PYINSPECT'
import glob, sys
import pandas as pd

out = open("DATA_PROFILE.md", "w")
cands = [f for f in glob.glob("*.csv") + glob.glob("data/*.csv") + glob.glob("dataset*.json") + glob.glob("data/*.json")]
if not cands:
    out.write("# Data Profile\n\nNo dataset found (looked for *.csv, data/*.csv, *.json, data/*.json).\n")
    out.close()
    sys.exit(0)
path = cands[0]
out.write(f"# Data Profile\n\nSource: `{path}`\n\n")
try:
    if path.endswith(".csv"):
        df = pd.read_csv(path)
    else:
        df = pd.read_json(path)
except Exception as exc:
    out.write(f"Load error: {exc}\n")
    out.close()
    sys.exit(0)
out.write(f"Rows: {len(df)}  Columns: {len(df.columns)}\n\n")
out.write("## Columns\n\n")
for c in df.columns:
    out.write(f"- `{c}`: {df[c].dtype} | non-null {df[c].notna().sum()} | ")
    if pd.api.types.is_numeric_dtype(df[c]):
        out.write(f"min {df[c].min()} max {df[c].max()} mean {df[c].mean():.3f}\n")
    else:
        out.write(f"unique {df[c].nunique()}\n")
out.write("\n## Head\n\n")
out.write(df.head(5).to_string())
out.write("\n")
out.close()
PYINSPECT
exit 0
"""


def _explore_adk_node() -> Node:
    """ADK LlmAgent: explore the data -> EXPLORATION.md."""
    def _run(inputs: dict, job_dir) -> dict:
        from nine.runtime.adk_runtime import ADKAgentNode

        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:500]
        _require_key("analyze (explore)")

        from google.adk.agents import LlmAgent
        from google.adk.models import Gemini
        from google.adk.tools import FunctionTool

        def write_file(path: str, content: str) -> str:
            """Write a file into the workspace (job dir)."""
            (job_dir / path).write_text(content, encoding="utf-8")
            return f"wrote {path} ({len(content)} bytes)"

        profile = ""
        p = job_dir / "DATA_PROFILE.md"
        if p.exists():
            profile = p.read_text(encoding="utf-8")[:3000]

        agent = LlmAgent(
            name="explorer",
            model=Gemini(model="gemini-3.6-flash"),
            instruction=(
                "You are the explorer of nine, an evidence-gated agent "
                "OS. Study the dataset profile below (plus the raw "
                "dataset file in the job dir if needed). Write "
                "EXPLORATION.md with:\n"
                "1. Patterns - 3-6 concrete patterns visible in the "
                "numbers (cite specific columns/values).\n"
                "2. Hypotheses - what might explain them.\n"
                "3. Anomalies - outliers, missing data, odd "
                "distributions.\n"
                "4. Chart recommendation - one chart that best tells the "
                "story (x, y, chart type).\n"
                "Ground every claim in the profile - no invented stats.\n"
                f"Task: {task}\n"
                f"DATA_PROFILE.md:\n{profile}\n"
            ),
            tools=[FunctionTool(write_file)],
        )
        return ADKAgentNode(agent)(inputs, job_dir)

    return Node(
        id="explore", kind="tool", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="ADK LlmAgent writes EXPLORATION.md (fails loud without a model)",
    )


def _visualize_command() -> str:
    """Bash (matplotlib): render chart.png from the dataset."""
    return r"""
export MPLBACKEND=Agg
python - <<'PYPLOT'
import glob, sys
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import pandas as pd

cands = [f for f in glob.glob("*.csv") + glob.glob("data/*.csv") + glob.glob("dataset*.json") + glob.glob("data/*.json")]
if not cands:
    open("chart.png", "wb").write(b"")
    sys.exit(0)
path = cands[0]
try:
    if path.endswith(".csv"):
        df = pd.read_csv(path)
    else:
        df = pd.read_json(path)
except Exception:
    open("chart.png", "wb").write(b"")
    sys.exit(0)

fig, ax = plt.subplots(figsize=(8, 5))
nums = df.select_dtypes(include="number").columns.tolist()
if len(nums) >= 2:
    ax.scatter(df[nums[0]], df[nums[1]], alpha=0.6)
    ax.set_xlabel(str(nums[0])); ax.set_ylabel(str(nums[1]))
    ax.set_title("Scatter: %s vs %s" % (nums[0], nums[1]))
elif len(nums) == 1:
    ax.hist(df[nums[0]], bins=20)
    ax.set_xlabel(str(nums[0])); ax.set_ylabel("count")
    ax.set_title("Histogram of %s" % nums[0])
else:
    vals = df.select_dtypes(include="object").iloc[:, 0].value_counts()
    ax.bar(vals.index.astype(str), vals.values)
    ax.set_title("Top categories")
    plt.xticks(rotation=45, ha="right")
fig.tight_layout()
fig.savefig("chart.png", dpi=100)
PYPLOT
exit 0
"""


def _report_prompt_node() -> Node:
    """Prompt node: synthesize INSIGHTS.md from profile + exploration."""
    def _run(inputs: dict, job_dir) -> dict:
        job_dir = Path(job_dir)
        task = str(inputs.get("task", ""))[:400]
        _require_key("analyze (report)")

        def _read(name: str, limit: int = 4000) -> str:
            p = job_dir / name
            return p.read_text(encoding="utf-8")[:limit] if p.exists() else "(missing)"

        prompt = (
            "You are the analyst of nine. Synthesize the dataset "
            "exploration into INSIGHTS.md with:\n"
            "1. Summary - 3-5 lines on what the data shows.\n"
            "2. Key insights - numbered, each with supporting evidence "
            "from DATA_PROFILE.md.\n"
            "3. Caveats - data quality limits.\n"
            "4. Next steps - analyses or models worth trying.\n"
            "Reference chart.png for the visual story.\n"
            f"Task: {task}\n"
            f"DATA_PROFILE.md:\n{_read('DATA_PROFILE.md', 3000)}\n"
            f"EXPLORATION.md:\n{_read('EXPLORATION.md')}\n"
        )
        insights = _gemini_generate(prompt, api_key=None)
        if not (insights and insights.strip()):
            raise WorkflowError(
                "analyze (report) model returned nothing - job failed "
                "loud (no offline fallback)"
            )
        (job_dir / "INSIGHTS.md").write_text(insights.strip(), encoding="utf-8")
        return {"output": "wrote INSIGHTS.md",
                "artifact_path": str(job_dir / "INSIGHTS.md")}

    return Node(
        id="report", kind="prompt", run=_run,
        max_retries=2, retry_delay_seconds=1.0,
        description="Prompt node writes INSIGHTS.md (fails loud without a model)",
    )


def _chart_check(ctx: dict[str, Any], workdir: Path) -> tuple[bool, str]:
    """chart.png must exist and be non-trivial (>= 1 KiB)."""
    p = Path(workdir) / "chart.png"
    if not p.exists():
        return False, "chart.png missing"
    size = p.stat().st_size
    if size < 1024:
        return False, f"chart.png too small ({size} bytes) - likely a stub"
    return True, f"chart.png present ({size} bytes)"


def analyze_hop() -> Hop:
    """The `analyze` workflow: dataset -> explore -> insights.

    Four-node hop:
      1. inspect (bash)     - DATA_PROFILE.md (pandas profile)
      2. explore (tool/ADK) - EXPLORATION.md (patterns + chart rec)
      3. visualize (bash)   - chart.png (matplotlib)
      4. report (prompt)    - INSIGHTS.md (synthesis)

    Gate: INSIGHTS.md non-empty + chart.png >= 1 KiB + artifacts + exits.
    """
    wf = Workflow(id="analyze",
                  description="Dataset -> explore -> insights")
    inspect = Node(id="inspect", kind="bash", command=_inspect_command(),
                   description="Profile the dataset (pandas) into DATA_PROFILE.md")
    explore = _explore_adk_node()
    explore.depends_on = ["inspect"]
    visualize = Node(id="visualize", kind="bash", command=_visualize_command(),
                     description="Render chart.png (matplotlib)")
    visualize.depends_on = ["explore"]
    report = _report_prompt_node()
    report.depends_on = ["visualize"]
    for n in (inspect, explore, visualize, report):
        wf.add_node(n)
    return Hop(
        id="analyze", workflow=wf,
        required_artifacts=[
            "DATA_PROFILE.md", "EXPLORATION.md", "chart.png", "INSIGHTS.md"
        ],
        gate_checks={
            "exit-codes": exit_codes_check(),
            "artifacts": required_artifact_check(
                ["DATA_PROFILE.md", "EXPLORATION.md", "chart.png",
                 "INSIGHTS.md"]
            ),
            "insights": file_nonempty_check("INSIGHTS.md", min_chars=100),
            "chart": _chart_check,
        },
        max_fix_loops=2,
    )
