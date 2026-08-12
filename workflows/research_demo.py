"""Example workflow — a 2-node "research + verify" DAG.

Workflows in chow-lite are DATA, not code: this file just constructs a
Workflow object (typed nodes + dependencies). The engine executes it in
topological order, registers artifacts, and runs the evidence gate.

Run it:
    python -m workflows.research_demo
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from chowlite.gates.evidence import EvidenceGate, exit_codes_check, required_artifact_check
from chowlite.ledger.ledger import JSONLLedger
from chowlite.runtime.workflows import Node, Workflow, WorkflowExecutor


def build_research_workflow() -> Workflow:
    wf = Workflow(id="research", description="Produce a findings document")
    wf.add_node(Node(
        id="collect", kind="bash",
        command="cat task.txt 2>/dev/null | head -3 > _src; "
                "echo '# Findings' > research.md; cat _src >> research.md",
    ))
    wf.add_node(Node(
        id="verify", kind="bash",
        command="test -s research.md && echo 'research.md present' > FINAL_REPORT.md",
        depends_on=["collect"],
    ))
    return wf


def main() -> int:
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        ledger = JSONLLedger(td / "ledger.jsonl")
        gate = EvidenceGate()
        gate.register_check("artifacts", required_artifact_check(["research.md"]))
        gate.register_check("exit-codes", exit_codes_check())

        job = ledger.submit("research", {"task": "demo"})
        job_dir = td / "work" / job.job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "task.txt").write_text("research the printing press\n")

        res = WorkflowExecutor(ledger, gate, workdir=td / "work").execute(
            build_research_workflow(), job, {"task": "demo"}
        )
        print(f"job={job.job_id} verdict={res['verdict']['verdict']}")
        print("artifacts:", [a["name"] for a in res["artifacts"]])
        return 0


if __name__ == "__main__":
    sys.exit(main())
