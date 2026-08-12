"""Chain engine — multi-hop department handoff with per-hop evidence gates.

A *chain* is an ordered sequence of hops. Each hop is a workflow with its
own evidence gate (the checks that must pass before the baton moves on):

    hop 1: research  -> research.md (findings)
    hop 2: plan      -> PLAN.md (build plan)
    hop 3: build     -> code + EVAL.json (implementation)
    hop 4: review    -> review.md (QA verdict on the build)
    hop 5: teach     -> TEACH.md (what was learned)

Artifacts produced by hop N live in the job directory and are handed to
hop N+1 via the artifact-passing contract (files are the interface).

If a hop's gate returns FIX, the hop re-runs (max_fix_loops). If it still
fails, the chain BLOCKs and the job is marked blocked — nothing ships
without evidence. This is the core doctrine: exit code is not success;
an artifact that cannot be verified does not ship.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from chowlite.gates.evidence import EvidenceGate
from chowlite.ledger.ledger import JSONLLedger, Job
from chowlite.runtime.workflows import Workflow, WorkflowExecutor, WorkflowError


class ChainError(Exception):
    pass


@dataclass
class Hop:
    """One stage of a chain: a workflow + the gate checks that must pass."""
    id: str
    workflow: Workflow
    required_artifacts: list[str] = field(default_factory=list)
    gate_checks: dict[str, Callable] = field(default_factory=dict)
    max_fix_loops: int = 2


@dataclass
class Chain:
    """An ordered sequence of hops sharing one job directory."""
    id: str
    hops: list[Hop]
    description: str = ""

    def hop(self, hop_id: str) -> Hop:
        for h in self.hops:
            if h.id == hop_id:
                return h
        raise ChainError(f"no hop named {hop_id}")


class ChainExecutor:
    """Runs each hop in order, enforcing per-hop gates and artifact handoff."""

    def __init__(
        self,
        ledger: JSONLLedger,
        workdir: str | Path = "work",
    ) -> None:
        self.ledger = ledger
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.results: dict[str, Any] = {}

    def _gate_for(self, hop: Hop) -> EvidenceGate:
        gate = EvidenceGate()
        for name, check in hop.gate_checks.items():
            gate.register_check(name, check)
        return gate

    def execute(self, chain: Chain, job: Job, inputs: dict[str, Any]) -> dict[str, Any]:
        """Run all hops. Returns per-hop verdicts + final chain verdict."""
        job_dir = self.workdir / job.job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        hop_results: dict[str, Any] = {}
        chain_inputs = dict(inputs)
        final = "BLOCKED"

        for idx, hop in enumerate(chain.hops):
            gate = self._gate_for(hop)
            ex = WorkflowExecutor(self.ledger, gate, workdir=self.workdir,
                                  job_dir_override=job_dir)
            # chain prefix on the workflow id so ledger records are traceable
            wf_id = f"{chain.id}::{hop.id}"

            attempt = 0
            verdict = None
            while attempt <= hop.max_fix_loops:
                attempt += 1
                hop_job = self.ledger.submit(wf_id, input=dict(chain_inputs), chain_id=chain.id)
                try:
                    res = ex.execute(hop.workflow, hop_job, chain_inputs)
                except WorkflowError as exc:
                    self.results[f"{hop.id}:{attempt}"] = {"error": str(exc)}
                    raise ChainError(f"hop {hop.id} crashed: {exc}") from exc

                verdict = res["verdict"]["verdict"]
                hop_results[f"{hop.id}:{attempt}"] = {
                    "verdict": verdict,
                    "job_id": hop_job.job_id,
                    "eval": res["verdict"]["eval_results"],
                }
                if verdict == "SHIP":
                    break
                # FIX: missing required artifacts / failed checks -> re-run
                missing = [a for a in hop.required_artifacts
                           if not (job_dir / a).exists()]
                if missing and attempt <= hop.max_fix_loops:
                    # inject the fix directive into the next attempt's inputs
                    chain_inputs["fix_directive"] = (
                        f"hop {hop.id} failed gate (attempt {attempt}): "
                        f"missing artifacts {missing}; rework and re-run."
                    )
                    continue
                break

            if verdict != "SHIP":
                self.results["final"] = {"verdict": "BLOCKED", "at_hop": hop.id}
                return {"final": "BLOCKED", "at_hop": hop.id,
                        "hop_results": hop_results}

            # hop shipped: hand artifacts (job dir files) to next hop and
            # roll the hop's artifacts up to the chain job for one ledger view
            for art in self.ledger.get(hop_job.job_id).artifacts:
                job.add_artifact(art)
            chain_inputs["hop_artifacts"] = {
                a: str(job_dir / a) for a in hop.required_artifacts
                if (job_dir / a).exists()
            }
            final = "SHIPPED"

        self.ledger.update(job)
        self.results["final"] = {"verdict": final, "hops": list(hop_results)}
        return {"final": final, "hop_results": hop_results}
