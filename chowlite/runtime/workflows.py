"""Workflow engine — the EXECUTE step of the chow-lite loop.

A workflow is a DAG of typed nodes:
    prompt  — LLM step (Gemini 3.5 Flash via ADK or raw API)
    bash    — deterministic shell step
    tool    — tool/function call (API, filesystem, etc.)
    subagent— nested agent run (ADK sub-agent)

Nodes produce artifacts that are passed to downstream nodes via the
artifact-passing contract (JSON schema validated). The engine tracks
the job in the ledger and runs the evidence gate at the end.

Design note: this is a *declarative* engine — workflows are data, not code.
"""
from __future__ import annotations

import hashlib
import subprocess as sp
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from chowlite.ledger.ledger import JSONLLedger, Job
from chowlite.gates.evidence import EvidenceGate


class WorkflowError(Exception):
    pass


@dataclass
class Node:
    """One typed node in a workflow DAG."""
    id: str
    kind: str  # prompt | bash | tool | subagent
    run: Optional[Callable[..., Any]] = None   # callable for tool/prompt nodes
    command: Optional[str] = None              # for bash nodes
    depends_on: list[str] = field(default_factory=list)
    timeout_seconds: int = 300
    description: str = ""


@dataclass
class Workflow:
    """A named DAG of typed nodes."""
    id: str
    nodes: dict[str, Node] = field(default_factory=dict)
    description: str = ""
    version: str = "0.1.0"

    def add_node(self, node: Node) -> "Workflow":
        self.nodes[node.id] = node
        return self

    def topological_order(self) -> list[str]:
        """Kahn's algorithm over depends_on edges."""
        order: list[str] = []
        visited: dict[str, bool] = {nid: False for nid in self.nodes}
        tmp: dict[str, bool] = {}

        def visit(nid: str) -> None:
            if visited[nid]:
                return
            if tmp.get(nid):
                raise WorkflowError(f"cycle detected at node {nid}")
            tmp[nid] = True
            for dep in self.nodes[nid].depends_on:
                if dep in self.nodes:
                    visit(dep)
            tmp[nid] = False
            visited[nid] = True
            order.append(nid)

        for nid in self.nodes:
            visit(nid)
        return order


class WorkflowExecutor:
    """Executes a workflow DAG, producing artifacts + a verdict.

    Args:
        ledger: JSONLLedger for job tracking
        gate: EvidenceGate for the final verdict
        workdir: directory where artifacts land
    """

    def __init__(
        self,
        ledger: JSONLLedger,
        gate: EvidenceGate,
        workdir: str | Path = "work",
    ) -> None:
        self.ledger = ledger
        self.gate = gate
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)

    def _hash(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _run_node(self, node: Node, inputs: dict[str, Any], job_dir: Path) -> dict[str, Any]:
        """Execute one node, returning {output, artifact?, ...}."""
        if node.kind == "bash":
            if not node.command:
                raise WorkflowError(f"node {node.id}: bash node needs command")
            res = sp.run(
                node.command, shell=True, cwd=job_dir, capture_output=True,
                text=True, timeout=node.timeout_seconds,
            )
            return {"exit_code": res.returncode, "stdout": res.stdout[-2000:],
                    "stderr": res.stderr[-2000:]}
        if node.kind in ("prompt", "tool", "subagent"):
            if node.run is None:
                raise WorkflowError(f"node {node.id}: {node.kind} node needs a callable")
            out = node.run(inputs, job_dir)
            return out if isinstance(out, dict) else {"output": out}
        raise WorkflowError(f"node {node.id}: unknown kind {node.kind}")

    def execute(self, workflow: Workflow, job: Job, inputs: dict[str, Any]) -> dict[str, Any]:
        """Run the workflow for a job. Returns the final verdict + artifacts."""
        # lifecycle: submitted -> routing -> running
        if job.status == "submitted":
            job.transition("routing")
            self.ledger.update(job)
        job.transition("running")
        job.attempts += 1
        self.ledger.update(job)

        job_dir = self.workdir / job.job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        node_outputs: dict[str, Any] = {}
        artifacts: list[dict[str, Any]] = []
        node_exit_codes: dict[str, int] = {}
        order = workflow.topological_order()

        for nid in order:
            node = workflow.nodes[nid]
            # gather upstream inputs
            node_inputs = {"task": inputs.get("task", ""), "node": nid}
            for dep in node.depends_on:
                if dep in node_outputs:
                    node_inputs[dep] = node_outputs[dep]
            # snapshot job dir to detect newly-produced artifacts
            before = {p.name for p in job_dir.iterdir() if p.is_file()}
            try:
                result = self._run_node(node, node_inputs, job_dir)
            except Exception as exc:  # noqa: BLE001
                job.transition("failed")
                self.ledger.update(job)
                raise WorkflowError(f"node {nid} failed: {exc}") from exc

            node_outputs[nid] = result
            if node.kind == "bash" and "exit_code" in result:
                node_exit_codes[nid] = result["exit_code"]

            # artifact registration: files newly created in the job dir by
            # this node (deterministic, schema-conformant manifest)
            for p in sorted(job_dir.iterdir()):
                if p.is_file() and p.name not in before:
                    data = p.read_bytes()
                    kind = "document"
                    if p.suffix in (".py", ".js", ".ts", ".go", ".sh", ".json", ".yaml", ".yml"):
                        kind = "code"
                    elif p.suffix in (".png", ".jpg", ".mp4", ".wav"):
                        kind = "media"
                    elif p.suffix in (".csv", ".jsonl", ".parquet", ".db", ".sqlite"):
                        kind = "data"
                    artifact = {
                        "name": p.name,
                        "path": str(p),
                        "kind": kind,
                        "sha256": self._hash(data),
                        "size": len(data),
                        "produced_by": nid,
                        "produced_at": job.updated_at,
                    }
                    artifacts.append(artifact)
                    job.add_artifact(artifact)

            # explicit artifact paths in node output (tool nodes)
            for key in ("artifact", "artifact_path"):
                val = result.get(key)
                if val:
                    p = Path(val) if isinstance(val, str) else val
                    if p.exists() and p.name not in {a["name"] for a in artifacts}:
                        data = p.read_bytes()
                        artifact = {
                            "name": p.name,
                            "path": str(p),
                            "kind": "other",
                            "sha256": self._hash(data),
                            "size": len(data),
                            "produced_by": nid,
                            "produced_at": job.updated_at,
                        }
                        artifacts.append(artifact)
                        job.add_artifact(artifact)

        job.transition("awaiting_evidence")
        self.ledger.update(job)

        # evidence gate
        artifact_ctx = {
            "artifact_paths": [a["path"] for a in artifacts],
            "artifacts": artifacts,
            "node_exit_codes": node_exit_codes,
        }
        verdict = self.gate.evaluate(artifact_ctx, job_dir)
        job.add_verdict(verdict)

        if verdict["verdict"] == "SHIP":
            job.transition("shipped")
        elif verdict["verdict"] == "FIX" and job.attempts <= job.max_fix_loops:
            job.transition("fixing")
        else:
            job.transition("blocked")
        self.ledger.update(job)

        return {
            "job_id": job.job_id,
            "verdict": verdict,
            "artifacts": artifacts,
            "node_outputs": {k: v for k, v in node_outputs.items()},
        }
