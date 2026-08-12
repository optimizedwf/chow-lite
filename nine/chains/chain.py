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

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from typing import Any

from nine.gates.evidence import EvidenceGate
from nine.learn.learner import RouteEvent
from nine.ledger.ledger import Job, JSONLLedger
from nine.runtime.workflows import Workflow, WorkflowError, WorkflowExecutor


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


def force_terminal(job: Job, status: str) -> None:
    """Drive a container job to a terminal status via legal transitions.

    The chain job is a container: hops run as their own ledger jobs, so we
    walk it through the legal path (submitted -> routing -> running -> ...)
    and set the status directly only if a transition is impossible.
    """
    from datetime import datetime

    path = {
        "shipped": ("routing", "running", "awaiting_evidence"),
        "blocked": ("routing", "running"),
        "failed": ("routing", "running"),
        "cancelled": (),
    }.get(status, ())
    for st in path:
        try:
            job.transition(st)
        except Exception:  # noqa: BLE001 - some states already passed
            pass
    try:
        job.transition(status)
    except Exception:  # noqa: BLE001 - fall back to direct set
        job.status = status
        job.updated_at = datetime.now(UTC).isoformat()


class ChainExecutor:
    """Runs each hop in order, enforcing per-hop gates and artifact handoff."""

    def __init__(
        self,
        ledger: JSONLLedger,
        workdir: str | Path = "work",
        learner=None,
    ) -> None:
        self.ledger = ledger
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.results: dict[str, Any] = {}
        self.learner = learner  # optional LEARN-loop observer

    def _gate_for(self, hop: Hop) -> EvidenceGate:
        gate = EvidenceGate()
        for name, check in hop.gate_checks.items():
            gate.register_check(name, check)
        return gate

    def execute(
        self,
        chain: Chain,
        job: Job,
        inputs: dict[str, Any],
        decision=None,
    ) -> dict[str, Any]:
        """Run all hops. Returns per-hop verdicts + final chain verdict.

        decision: the RouteDecision that selected this chain (P1-5 — chain
        route events must carry the REAL confidence/router version, not a
        hardcoded placeholder).
        """
        try:
            return self._execute(chain, job, inputs, decision=decision)
        except Exception:
            try:
                force_terminal(job, "failed")
                self.ledger.update(job)
            except Exception:  # noqa: BLE001
                pass
            raise

    def _execute(
        self,
        chain: Chain,
        job: Job,
        inputs: dict[str, Any],
        decision=None,
    ) -> dict[str, Any]:
        """Run all hops. Returns per-hop verdicts + final chain verdict."""
        job_dir = self.workdir / job.job_id
        job_dir.mkdir(parents=True, exist_ok=True)

        # P1-5: carry the real ROUTE decision (confidence, router version)
        # onto the chain job and into every hop's route event. If the caller
        # didn't supply one, derive a deterministic keyword decision from
        # the shared registry so the LEARN loop always sees real values.
        if decision is None:
            from nine.registry import HOP_DESCRIPTIONS, KEYWORDS
            from nine.router.classifier import Router

            _r = Router()
            for wf_id, kws in KEYWORDS.items():
                _r.register(wf_id, kws, HOP_DESCRIPTIONS.get(wf_id, ""))
            decision = _r.classify(str(inputs.get("task", "")))
        if job.route_decision is None:
            job.attach_route_decision(decision)
            self.ledger.update(job)

        hop_results: dict[str, Any] = {}
        chain_inputs = dict(inputs)
        final = "BLOCKED"

        for _idx, hop in enumerate(chain.hops):
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
                # LEARN: record the route event for the learning loop
                # (P1-5: real confidence/router_version from the ROUTE step;
                # task text is redact()ed, not just truncated)
                if self.learner is not None:
                    from nine.router.classifier import redact

                    self.learner.observe(
                        RouteEvent(
                            event_id=f"ev-{hop_job.job_id[:8]}",
                            job_id=hop_job.job_id,
                            task_redacted=redact(str(inputs.get("task", "")))[:200],
                            workflow_id=wf_id,
                            confidence=float(decision.confidence),
                            router_version=decision.router_version,
                            verdict=verdict,
                            checks_passed=sum(
                                1 for r in res["verdict"]["eval_results"].values()
                                if r.get("passed")
                            ),
                            checks_total=len(res["verdict"]["eval_results"]),
                            fix_directive=inputs.get("fix_directive", ""),
                        )
                    )
                if verdict == "SHIP":
                    break
                # FIX: any non-SHIP gate verdict retries while attempts remain
                # (missing artifacts OR failing EVAL.json checks both re-run)
                if attempt <= hop.max_fix_loops:
                    missing = [a for a in hop.required_artifacts
                               if not (job_dir / a).exists()]
                    reason = (f"missing artifacts {missing}" if missing
                              else "gate checks failed")
                    chain_inputs["fix_directive"] = (
                        f"hop {hop.id} failed gate (attempt {attempt}): "
                        f"{reason}; rework and re-run."
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

        # chain job reaches a terminal state in the durable ledger (was
        # staying 'submitted' forever); mark failed on any crash too.
        force_terminal(job, "shipped" if final == "SHIPPED" else "blocked")
        self.ledger.update(job)
        self.results["final"] = {"verdict": final, "hops": list(hop_results)}
        return {"final": final, "hop_results": hop_results}
