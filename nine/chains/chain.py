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

import sys
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
        # Slice-51 armor: the direct-set fallback must stamp completed_at
        # for terminal statuses, exactly like Job.transition does —
        # a force-blocked/failed/cancelled chain job that never gets
        # completed_at looks permanently in-flight to discover/status.
        job.status = status
        job.updated_at = datetime.now(UTC).isoformat()
        if status in ("shipped", "blocked", "failed", "cancelled", "archived"):
            job.completed_at = job.updated_at


class ChainExecutor:
    """Runs each hop in order, enforcing per-hop gates and artifact handoff."""

    def __init__(
        self,
        ledger: JSONLLedger,
        workdir: str | Path = "work",
        learner=None,
        memory=None,
    ) -> None:
        self.ledger = ledger
        self.workdir = Path(workdir)
        self.workdir.mkdir(parents=True, exist_ok=True)
        self.results: dict[str, Any] = {}
        self.learner = learner  # optional LEARN-loop observer
        self.memory = memory  # optional semantic MemoryGraph (artifact summaries)

    def _cancelled(self, job: Job) -> bool:
        """Fresh ledger read: did an operator cancel this job cross-process?

        The in-memory job copy is last-line-wins; a cancel from another
        process appends a `cancelled` line this object never sees. Reload
        the ledger file and compare the durable status (torture-8 F3).
        """
        try:
            live = self.ledger.refresh(job.job_id)
        except Exception:  # noqa: BLE001 - best-effort poll
            return False
        return live.status == "cancelled"

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
            # torture-7 F4: an explicit chain invocation (CLI `nine chain`,
            # recover of a chain job) must NOT fabricate a keyword decision —
            # classifying the task stamped workflow_id="respond" with
            # confidence 0.0 onto a chain job that ran a chain, and every
            # hop LEARN event then polluted the router catalog with bogus
            # low-confidence entries. Honest decision: the chain id, 1.0.
            from datetime import UTC, datetime
            from uuid import uuid4

            from nine.router.classifier import RouteDecision, redact

            decision = RouteDecision(
                decision_id=str(uuid4()),
                task_redacted=redact(str(inputs.get("task", "")))[:500],
                workflow_id=chain.id,
                confidence=1.0,
                reason="explicit chain invocation",
                decided_at=datetime.now(UTC).isoformat(),
                router_version="explicit-chain",
                model="explicit-chain",
            )
        if job.route_decision is None:
            job.attach_route_decision(decision)
            self.ledger.update(job)

        hop_results: dict[str, Any] = {}
        chain_inputs = dict(inputs)
        final = "BLOCKED"

        for _idx, hop in enumerate(chain.hops):
            # torture-8 F3: an operator cancel (cross-process ledger append)
            # must stop the chain between hops, not after all hops ran.
            if self._cancelled(job):
                force_terminal(job, "cancelled")
                self.ledger.update(job)
                self.results["final"] = {"verdict": "CANCELLED", "at_hop": hop.id}
                return {"final": "CANCELLED", "at_hop": hop.id,
                        "hop_results": hop_results}
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
                    # hop-level FIX loop owns retries here; the engine's
                    # in-engine loop is for single-workflow runs
                    res = ex.execute(hop.workflow, hop_job, chain_inputs, fix_loop=False)
                except WorkflowError as exc:
                    self.results[f"{hop.id}:{attempt}"] = {"error": str(exc)}
                    raise ChainError(f"hop {hop.id} crashed: {exc}") from exc

                verdict = res["verdict"]["verdict"]
                if verdict == "CANCELLED":
                    # operator cancelled mid-hop: stop the whole chain
                    # without stamping shipped over the cancel (torture-8 F3)
                    force_terminal(job, "cancelled")
                    self.ledger.update(job)
                    self.results["final"] = {"verdict": "CANCELLED", "at_hop": hop.id}
                    return {"final": "CANCELLED", "at_hop": hop.id,
                            "hop_results": hop_results}
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

                    try:
                        self.learner.observe(
                            RouteEvent(
                                event_id=f"ev-{hop_job.job_id[:8]}"
                                           f"-{int((hop_job.metadata or {}).get('run_seq', 0))}",
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
                    except OSError as exc:
                        # torture-21 F1: LEARN is best-effort AFTER the hop
                        # verdict is durable — a broken events store must
                        # not fail the chain (raw traceback) nor 500 the
                        # server on an already-shipped hop.
                        print(f"WARNING: route-event write skipped ({exc}); "
                              "hop verdict already durable", file=sys.stderr)
                if verdict == "SHIP":
                    # torture-12 F2: the FIX directive belongs to THIS hop's
                    # retries - once the hop ships it must NOT bleed into
                    # later hops' prompts (flagship ADK nodes append
                    # "Previous attempt failed the gate: ..." to their
                    # instruction on inputs.get("fix_directive")).
                    chain_inputs.pop("fix_directive", None)
                    break
                # FIX: any non-SHIP gate verdict retries while attempts remain
                # (missing artifacts OR failing EVAL.json checks both re-run)
                if attempt <= hop.max_fix_loops:
                    missing = [a for a in hop.required_artifacts
                               if not (job_dir / a).exists()]
                    # torture-29 F3: the chain FIX directive used to say
                    # only "gate checks failed" — dropping the failing check
                    # names/messages that res["verdict"]["eval_results"]
                    # already carries (workflows.py enumerates them:
                    # "gate FIX after attempt N: <k>: <message>; ...").
                    # Flagship ADK hops expose only a write_file tool (no
                    # read tool), so this directive is the retry's ONLY
                    # signal — "gate checks failed" made retries blind
                    # (T7-F2: the directive must name what failed).
                    failures = [
                        f"{k}: {v['message']}"
                        for k, v in res["verdict"]["eval_results"].items()
                        if not v.get("passed")
                    ]
                    reason = (f"missing artifacts {missing}" if missing
                              else ("; ".join(failures) if failures
                                     else "gate checks failed"))
                    chain_inputs["fix_directive"] = (
                        f"hop {hop.id} failed gate (attempt {attempt}): "
                        f"{reason}; rework and re-run."
                    )
                    continue
                break

            if verdict != "SHIP":
                # torture-5 F3: the BLOCKED early return used to skip the
                # terminal-state walk, leaving the container job 'submitted'
                # forever (discover --status blocked missed it and recover
                # refused it). Mark blocked in the durable ledger here too.
                force_terminal(job, "blocked")
                self.ledger.update(job)
                self.results["final"] = {"verdict": "BLOCKED", "at_hop": hop.id}
                return {"final": "BLOCKED", "at_hop": hop.id,
                        "hop_results": hop_results}

            # hop shipped: hand artifacts (job dir files) to next hop and
            # roll the hop's artifacts up to the chain job for one ledger view
            for art in self.ledger.get(hop_job.job_id).artifacts:
                job.add_artifact(art)
            if self.memory is not None:
                try:
                    self._save_memory(hop_job, chain, hop, job_dir, verdict,
                                      inputs)
                except OSError as exc:
                    # torture-21 F1: memory is best-effort after the hop
                    # verdict is durable (see _save_memory's own write
                    # guard) — belt for store-level failures.
                    print(f"WARNING: memory write skipped ({exc}); "
                          "hop verdict already durable", file=sys.stderr)
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
    def _save_memory(
        self,
        hop_job: Job,
        chain: Chain,
        hop: Hop,
        job_dir: Path,
        verdict: str,
        inputs: dict[str, Any],
    ) -> None:
        """Record the hop's semantic summary + artifact lineage (write-path:
        distill then store, never raw conversation)."""
        from nine.router.classifier import redact

        # torture-14 F2: the memory write-path contract is "distill then
        # store, never raw". This used to stamp the plan hop's RAW HANDOFF.md
        # (unredacted, up to 2000 chars) as the summary for EVERY artifact of
        # EVERY hop — credentials the model echoed from the raw task landed
        # verbatim in memory.jsonl (Firestore on Cloud Run), and build/review/
        # teach entries misattributed the plan handoff as their own content.
        # Now: redact() every summary and use the ARTIFACT's own content head
        # (falling back to the plan handoff only for the handoff artifact
        # itself / when the artifact file is gone).
        def _artifact_summary(art: dict) -> str:
            # torture-15 F7: resolve from the REGISTERED artifact path FIRST
            # — an outside-job-dir artifact ("../<name>") was previously
            # summarized from HANDOFF.md (or job_dir/<name>, which doesn't
            # exist), misattributing the plan handoff as ITS content. The
            # manifest's path IS the artifact's own file; read it (redacted
            # head) whenever it exists and is a regular file. job_dir/<name>
            # is the next candidate; HANDOFF.md is the LAST resort, used
            # only when the artifact file is genuinely gone.
            candidates = [art.get("path")]
            if art.get("path"):
                candidates.append(str(job_dir / art["path"]))
            candidates.append(str(job_dir / art.get("name", "")))
            for cand in candidates:
                if not cand:
                    continue
                src = Path(cand)
                if src.exists() and not src.is_symlink():
                    try:
                        head = src.read_text(encoding="utf-8",
                                             errors="replace")[:400]
                    except OSError:
                        head = ""
                    if head.strip():
                        return redact(head)
            handoff = job_dir / "HANDOFF.md"
            if handoff.exists() and not handoff.is_symlink():
                try:
                    head = handoff.read_text(encoding="utf-8",
                                             errors="replace")[:400]
                except OSError:
                    head = ""
                if head.strip():
                    return redact(head)
            return redact(str(art.get("name", "")))
        task_redacted = redact(str(inputs.get("task", "")))[:200]
        for art in self.ledger.get(hop_job.job_id).artifacts:
            self.memory.save_artifact_summary(
                job_id=hop_job.job_id,
                chain_id=chain.id,
                hop_id=hop.id,
                workflow_id=hop_job.workflow_id,
                artifact_name=art["name"],
                kind=art.get("kind", "document"),
                sha256=art.get("sha256", ""),
                size=art.get("size", 0),
                summary=_artifact_summary(art),
                task_redacted=task_redacted,
                verdict=verdict,
            )

