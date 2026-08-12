"""chow-lite CLI — the operator interface.

    chow submit  "<task>"           submit a task (router -> workflow -> run)
    chow chain <chain_id> "<task>"  run a multi-hop chain (flagship / demo)
    chow status  <job_id>           job status
    chow discover [--status X]      list jobs
    chow artifacts <job_id>         list job artifacts
    chow cancel <job_id>            cancel a job
    chow recover <job_id>           recover a blocked/failed job
    chow stats                      ledger stats

Chains:
    flagship   research -> plan -> build -> review -> teach (5 hops)
    demo       inbox -> triage -> task -> report (demo lane)

Exit codes: 0 ok, 1 error. (An exit code is NOT task success — check
`chow status` for the SHIP/FIX/BLOCK verdict.)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from chowlite.gates.evidence import (
    EvidenceGate,
    eval_json_check,
    exit_codes_check,
    required_artifact_check,
)
from chowlite.ledger.ledger import JSONLLedger, LedgerError
from chowlite.router.classifier import Router
from chowlite.runtime.workflows import Node, Workflow, WorkflowExecutor, write_demo_artifacts

DEFAULT_LEDGER = "jobs/ledger.jsonl"


def _ledger(args) -> JSONLLedger:
    return JSONLLedger(args.ledger)


def build_default_router() -> Router:
    from chowlite.registry import HOP_DESCRIPTIONS, KEYWORDS
    r = Router()
    for wf_id, kws in KEYWORDS.items():
        r.register(wf_id, kws, HOP_DESCRIPTIONS.get(wf_id, ""))
    return r


def cmd_chain(args) -> int:
    """Run a named chain end-to-end with per-hop evidence gates."""
    from chowlite.chains.chain import ChainExecutor
    from chowlite.chains.flagship import demo_lane, research_plan_build_review_teach

    chains = {
        "flagship": research_plan_build_review_teach,
        "demo": demo_lane,
        "inbox-triage-task-report": demo_lane,
        "research-plan-build-review-teach": research_plan_build_review_teach,
    }
    if args.chain_id not in chains:
        print(f"unknown chain '{args.chain_id}'; choices: {sorted(chains)}", file=sys.stderr)
        return 1

    ledger = _ledger(args)
    chain = chains[args.chain_id]()
    ex = ChainExecutor(ledger, workdir=args.workdir)

    # seed the chain job dir with the task input file
    job = ledger.submit(chain.id, input={"task": args.task})
    job_dir = Path(args.workdir) / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text(args.task + "\n")
    if chain.id == "inbox-triage-task-report":
        (job_dir / "inbox.txt").write_text(args.task + "\n")

    res = ex.execute(chain, job, {"task": args.task})
    print(f"chain={chain.id} job={job.job_id} final={res['final']}")
    for hop, info in res["hop_results"].items():
        print(f"  {hop}: {info['verdict']}")
    print("\nartifacts:")
    for a in ledger.get(job.job_id).artifacts:
        print(f"  {a['name']}  {a['sha256'][:12]}  {a['size']}B  by {a['produced_by']}")
    return 0 if res["final"] == "SHIPPED" else 2


def build_default_gate() -> EvidenceGate:
    gate = EvidenceGate()
    gate.register_check("eval-json", eval_json_check())
    gate.register_check("artifacts", required_artifact_check(["FINAL_REPORT.md"]))
    gate.register_check("exit-codes", exit_codes_check())
    return gate


def cmd_submit(args) -> int:
    ledger = _ledger(args)
    router = build_default_router()
    decision = router.classify(args.task)
    print(json.dumps(decision.to_dict(), indent=2))

    if decision.workflow_id in ("respond", "fallback-respond"):
        print("\n[direct answer] no execution run needed:", decision.reason)
        return 0

    job = ledger.submit(workflow_id=decision.workflow_id, input={"task": args.task})
    job.attach_route_decision(decision)
    ledger.update(job)

    # dispatch through the shared registry: real workflows/chains per
    # workflow_id (research != review != build); Python collect node is the
    # RCE-hardened fallback for unregistered ids.
    from chowlite.registry import CHAINS, WORKFLOWS

    if decision.workflow_id in CHAINS:
        from chowlite.chains.chain import ChainExecutor
        chain = CHAINS[decision.workflow_id]()
        cex = ChainExecutor(ledger, workdir=args.workdir)
        job_dir = Path(args.workdir) / job.job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "task.txt").write_text(args.task + "\n")
        if decision.workflow_id == "inbox-triage-task-report":
            (job_dir / "inbox.txt").write_text(args.task + "\n")
        res = cex.execute(chain, job, {"task": args.task})
        print(f"chain={chain.id} job={job.job_id} final={res['final']}")
        return 0 if res["final"] == "SHIPPED" else 2

    if decision.workflow_id in WORKFLOWS:
        wf = WORKFLOWS[decision.workflow_id]()
    else:
        wf = Workflow(id=decision.workflow_id)
        wf.add_node(Node(id="collect", kind="tool",
                         run=lambda inputs, jd: write_demo_artifacts(
                             decision.workflow_id, args.task, Path(jd)),
                         description="collect task + write report artifact + EVAL.json (Python)"))

    gate = build_default_gate()
    executor = WorkflowExecutor(ledger, gate)
    result = executor.execute(wf, job, {"task": args.task})
    print("\n[verdict]", result["verdict"]["verdict"], "-", result["verdict"]["summary"])
    print("[job]", job.job_id, "->", job.status)
    return 0


def cmd_status(args) -> int:
    try:
        job = _ledger(args).get(args.job_id)
    except LedgerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(job.to_dict(), indent=2))
    return 0


def cmd_discover(args) -> int:
    jobs = _ledger(args).discover(status=args.status)
    for j in jobs:
        print(f"{j.job_id[:8]}  {j.workflow_id:12s} {j.status:10s} {j.created_at}")
    print(f"\n{len(jobs)} job(s)")
    return 0


def cmd_artifacts(args) -> int:
    try:
        arts = _ledger(args).artifacts(args.job_id)
    except LedgerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    for a in arts:
        print(f"{a['name']}  {a['sha256'][:12]}  {a['size']}B  by {a['produced_by']}")
    return 0


def cmd_cancel(args) -> int:
    job = _ledger(args).cancel(args.job_id)
    print(f"cancelled {job.job_id} -> {job.status}")
    return 0


def cmd_recover(args) -> int:
    job = _ledger(args).recover(args.job_id)
    print(f"recovered {job.job_id} -> {job.status}")
    return 0


def cmd_stats(args) -> int:
    print(json.dumps(_ledger(args).stats(), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="chow", description="chow-lite agent OS")
    p.add_argument("--ledger", default=DEFAULT_LEDGER, help="ledger path")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("submit")
    s.add_argument("task")
    s.set_defaults(fn=cmd_submit)

    s = sub.add_parser("chain")
    s.add_argument("chain_id")
    s.add_argument("task")
    s.add_argument("--ledger", default=DEFAULT_LEDGER)
    s.add_argument("--workdir", default="work")
    s.set_defaults(fn=cmd_chain)

    s = sub.add_parser("status")
    s.add_argument("job_id")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("discover")
    s.add_argument("--status", default=None)
    s.set_defaults(fn=cmd_discover)

    s = sub.add_parser("artifacts")
    s.add_argument("job_id")
    s.set_defaults(fn=cmd_artifacts)

    s = sub.add_parser("cancel")
    s.add_argument("job_id")
    s.set_defaults(fn=cmd_cancel)

    s = sub.add_parser("recover")
    s.add_argument("job_id")
    s.set_defaults(fn=cmd_recover)

    s = sub.add_parser("stats")
    s.set_defaults(fn=cmd_stats)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
