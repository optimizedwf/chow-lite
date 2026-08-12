"""Workflow engine — the EXECUTE step of the nine loop.

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
import os
import random
import subprocess as sp
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import Any

from nine.gates.evidence import EvidenceGate
from nine.ledger.ledger import Job, JSONLLedger


class WorkflowError(Exception):
    pass


@dataclass
class Node:
    """One typed node in a workflow DAG."""
    id: str
    kind: str  # prompt | bash | tool | subagent
    run: Callable[..., Any] | None = None   # callable for tool/prompt nodes
    command: str | None = None              # for bash nodes
    depends_on: list[str] = field(default_factory=list)
    timeout_seconds: int = 300
    max_retries: int = 0                    # transient-failure retries (backoff)
    retry_delay_seconds: float = 1.0        # base delay; doubles per retry
    retry_on_exit: bool = False             # bash: retry on non-zero exit code
    description: str = ""


@dataclass
class Workflow:
    """A named DAG of typed nodes."""
    id: str
    nodes: dict[str, Node] = field(default_factory=dict)
    description: str = ""
    version: str = "0.1.0"

    def add_node(self, node: Node) -> Workflow:
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
        job_dir_override: str | Path | None = None,
    ) -> None:
        self.ledger = ledger
        self.gate = gate
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        # chains run every hop in ONE shared directory so artifacts hand off
        self.job_dir_override = Path(job_dir_override) if job_dir_override else None

    def _hash(self, data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def _run_node_once(self, node: Node, inputs: dict[str, Any], job_dir: Path) -> dict[str, Any]:
        """Execute one node ONCE, returning {output, artifact?, ...}."""
        if node.kind == "bash":
            if not node.command:
                raise WorkflowError(f"node {node.id}: bash node needs command")
            # bash nodes resolve `python` to the SAME interpreter running
            # nine (venv), so scripts can rely on project deps (pandas,
            # matplotlib) without hard-coding an absolute interpreter path.
            bash_env = dict(os.environ)
            pybin = str(Path(sys.executable).parent)
            bash_env["PATH"] = pybin + os.pathsep + bash_env.get("PATH", "")
            res = sp.run(
                node.command, shell=True, cwd=job_dir, capture_output=True,
                text=True, timeout=node.timeout_seconds, check=False,
                env=bash_env,
            )
            return {"exit_code": res.returncode, "stdout": res.stdout[-2000:],
                    "stderr": res.stderr[-2000:]}
        if node.kind in ("prompt", "tool", "subagent", "summarize"):
            if node.run is None:
                raise WorkflowError(f"node {node.id}: {node.kind} node needs a callable")
            out = node.run(inputs, job_dir)
            return out if isinstance(out, dict) else {"output": out}
        raise WorkflowError(f"node {node.id}: unknown kind {node.kind}")

    def _run_node(self, node: Node, inputs: dict[str, Any], job_dir: Path) -> tuple[dict[str, Any], int]:
        """Run a node with retry/backoff for transient failures.

        Retries on any raised exception (timeout, Gemini 429/503, flaky
        tool) and — for bash nodes with retry_on_exit — on non-zero exit
        codes. Backoff = retry_delay_seconds * 2**attempt (+/-10% jitter).
        Returns (result, attempts_used).
        """
        attempts = 0
        while True:
            attempts += 1
            try:
                result = self._run_node_once(node, inputs, job_dir)
                if (
                    node.kind == "bash"
                    and node.retry_on_exit
                    and result.get("exit_code", 0) != 0
                    and attempts <= node.max_retries
                ):
                    time.sleep(self._backoff(node, attempts))
                    continue
                return result, attempts
            except WorkflowError:
                # Deterministic failure (no key, bad input, missing callable) —
                # retrying cannot fix it. Fail loud immediately.
                raise
            except Exception as exc:  # noqa: BLE001 — transient failures retried
                if attempts > node.max_retries:
                    raise WorkflowError(f"node {node.id} failed after {attempts} attempts: {exc}") from exc
                time.sleep(self._backoff(node, attempts))

    @staticmethod
    def _backoff(node: Node, attempt: int) -> float:
        base = node.retry_delay_seconds * (2 ** (attempt - 1))
        return base * (1 + random.uniform(-0.1, 0.1))

    def execute(
        self,
        workflow: Workflow,
        job: Job,
        inputs: dict[str, Any],
        fix_loop: bool = True,
    ) -> dict[str, Any]:
        """Run the workflow for a job, with an in-engine FIX loop.

        A FIX verdict re-runs the workflow (up to job.max_fix_loops) with a
        `fix_directive` describing exactly which gate checks failed — the
        single-workflow path (`nine submit`, POST /v1/submit) self-heals
        instead of leaving jobs stuck at `fixing`. Chains pass fix_loop=False
        (they re-run whole hops at the hop level).

        Returns the final verdict + artifacts + per-node timing/attempts.
        """
        inputs = dict(inputs)
        # lifecycle: submitted -> routing (once per job)
        if job.status == "submitted":
            job.transition("routing")
            self.ledger.update(job)

        job_dir = self.job_dir_override or (self.workdir / job.job_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        order = workflow.topological_order()

        node_outputs: dict[str, Any] = {}
        node_meta: dict[str, dict[str, Any]] = {}
        verdict: dict[str, Any] = {"verdict": "BLOCK"}
        attempt = 0

        while True:
            attempt += 1
            job.attempts += 1
            job.transition("running")
            job.artifacts = []  # manifest = this attempt's artifacts only
            seen: dict[str, str] = {}  # reset per attempt: reruns re-register the full dir
            self.ledger.update(job)

            artifacts: list[dict[str, Any]] = []
            node_exit_codes: dict[str, int] = {}

            for nid in order:
                node = workflow.nodes[nid]
                node_inputs = {
                    "task": inputs.get("task", ""),
                    "node": nid,
                    "job_id": job.job_id,
                    "attempt": attempt,
                    "fix_directive": inputs.get("fix_directive", ""),
                }
                for dep in node.depends_on:
                    if dep in node_outputs:
                        node_inputs[dep] = node_outputs[dep]
                started = monotonic()
                try:
                    result, attempts_used = self._run_node(node, node_inputs, job_dir)
                except Exception as exc:
                    job.transition("failed")
                    self.ledger.update(job)
                    raise WorkflowError(f"node {nid} failed: {exc}") from exc
                duration_ms = round((monotonic() - started) * 1000)
                node_meta[nid] = {
                    "attempts": attempts_used,
                    "duration_ms": duration_ms,
                    "node_attempt": attempt,
                }
                node_outputs[nid] = result
                if node.kind == "bash" and "exit_code" in result:
                    node_exit_codes[nid] = result["exit_code"]

                # artifact registration: name+content-deduped across attempts
                # (a FIX rerun that rewrites a file refreshes its sha256)
                for p in sorted(job_dir.iterdir()):
                    if not p.is_file():
                        continue
                    data = p.read_bytes()
                    h = self._hash(data)
                    if seen.get(p.name) == h:
                        continue
                    seen[p.name] = h
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
                        "sha256": h,
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
                        if p.exists() and seen.get(p.name) != self._hash(p.read_bytes()):
                            data = p.read_bytes()
                            h = self._hash(data)
                            seen[p.name] = h
                            artifact = {
                                "name": p.name,
                                "path": str(p),
                                "kind": "other",
                                "sha256": h,
                                "size": len(data),
                                "produced_by": nid,
                                "produced_at": job.updated_at,
                            }
                            artifacts.append(artifact)
                            job.add_artifact(artifact)

            job.transition("awaiting_evidence")
            self.ledger.update(job)

            artifact_ctx = {
                "artifact_paths": [a["path"] for a in artifacts],
                "artifacts": artifacts,
                "node_exit_codes": node_exit_codes,
            }
            verdict = self.gate.evaluate(artifact_ctx, job_dir)
            job.add_verdict(verdict)

            if verdict["verdict"] == "SHIP":
                job.transition("shipped")
                self.ledger.update(job)
                break

            if verdict["verdict"] == "FIX" and fix_loop and job.attempts <= job.max_fix_loops:
                job.transition("fixing")
                self.ledger.update(job)
                failures = [
                    f"{k}: {v['message']}"
                    for k, v in verdict["eval_results"].items()
                    if not v.get("passed")
                ]
                inputs["fix_directive"] = (
                    f"gate FIX after attempt {attempt}: "
                    + ("; ".join(failures) if failures else verdict.get("summary", ""))
                    + ". Rework the artifacts and re-run."
                )
                continue

            job.transition("blocked")
            self.ledger.update(job)
            break

        job.metadata["nodes"] = node_meta
        job.metadata["attempts"] = attempt
        self.ledger.update(job)
        return {
            "job_id": job.job_id,
            "verdict": verdict,
            "artifacts": artifacts,
            "attempts": attempt,
            "node_outputs": {k: v for k, v in node_outputs.items()},
            "node_meta": dict(node_meta),
        }


