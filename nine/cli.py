"""nine CLI — the operator interface.

    nine submit  "<task>"           submit a task (router -> workflow -> run)
    nine chain <chain_id> "<task>"  run a multi-hop chain (flagship / demo)
    nine status  <job_id>           job status
    nine discover [--status X]      list jobs
    nine artifacts <job_id>         list job artifacts
    nine cancel <job_id>            cancel a job
    nine recover <job_id>           recover a blocked/failed job
    nine stats                      ledger stats
    nine memory search <query>     search distilled hop summaries
    nine memory list               recent semantic memories

Chains:
    flagship   research -> plan -> build -> review -> teach (5 hops)
    demo       inbox -> triage -> task -> report (demo lane)

Exit codes: 0 ok, 1 error. (An exit code is NOT task success — check
`nine status` for the SHIP/FIX/BLOCK verdict.)
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from nine.gates.evidence import (
    EvidenceGate,
    eval_json_check,
    exit_codes_check,
)
from nine.ledger.ledger import JSONLLedger, LedgerError
from nine.router.classifier import Router
from nine.runtime.workflows import WorkflowError, WorkflowExecutor

DEFAULT_LEDGER = "jobs/ledger.jsonl"


def _ledger(args) -> JSONLLedger:
    return JSONLLedger(args.ledger)


def build_default_router() -> Router:
    from nine.registry import HOP_DESCRIPTIONS, KEYWORDS
    r = Router()
    for wf_id, kws in KEYWORDS.items():
        r.register(wf_id, kws, HOP_DESCRIPTIONS.get(wf_id, ""))
    return r


def cmd_memory(args) -> int:
    """Semantic memory: search distilled hop summaries / list recent entries."""
    from nine.memory.graph import get_memory_graph

    mem = get_memory_graph(path=args.memory)
    if mem is None:
        print("memory disabled (NINE_MEMORY=none)", file=sys.stderr)
        return 1
    if args.action == "search":
        if not args.query:
            print("usage: nine memory search <query>", file=sys.stderr)
            return 1
        hits = mem.search_context(args.query, k=10)
        if not hits:
            print(f"no memory entries match '{args.query}'")
            return 0
        for h in hits:
            print(f"{h['memory_id']}  [{h['hop_id']}] {h['artifact_name']}  "
                  f"verdict={h['verdict']}  {h['created_at'][:19]}")
            print(f"    {h['summary'][:160].replace(chr(10), ' ')}")
        return 0
    # list
    rows = []
    local_path = getattr(mem, "path", None)
    if isinstance(local_path, Path) and local_path.exists():
        import json as _json

        for line in reversed(open(local_path, encoding="utf-8").read().splitlines()):
            if line.strip():
                rows.append(_json.loads(line))
    else:
        rows = list(mem.search_context("latest", k=10))
    if not rows:
        print("memory is empty (run a chain to record hop summaries)")
        return 0
    for h in rows[-10:][::-1]:
        print(f"{h['memory_id']}  [{h['chain_id']}::{h['hop_id']}] "
              f"{h['artifact_name']}  verdict={h['verdict']}")
    print(f"\n{len(rows)} memory entries (last 10 shown)")
    return 0


def cmd_chain(args) -> int:
    """Run a named chain end-to-end with per-hop evidence gates."""
    from nine.chains.chain import ChainExecutor
    from nine.chains.flagship import demo_lane, research_plan_build_review_teach

    chains = {
        "flagship": research_plan_build_review_teach,
        "demo": demo_lane,
        "inbox-triage-task-report": demo_lane,
        "research-plan-build-review-teach": research_plan_build_review_teach,
    }
    if args.chain_id not in chains:
        print(f"unknown chain '{args.chain_id}'; choices: {sorted(chains)}", file=sys.stderr)
        return 1

    from nine.memory.graph import get_memory_graph

    ledger = _ledger(args)
    chain = chains[args.chain_id]()
    ex = ChainExecutor(ledger, workdir=args.workdir, learner=_learner(args),
                       memory=get_memory_graph(path=args.memory))

    # seed the chain job dir with the task input file
    job = ledger.submit(chain.id, input={"task": args.task})
    job_dir = Path(args.workdir) / job.job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "task.txt").write_text(args.task + "\n")
    if chain.id == "inbox-triage-task-report":
        (job_dir / "inbox.txt").write_text(args.task + "\n")

    from nine.chains.chain import ChainError

    try:
        res = ex.execute(chain, job, {"task": args.task})
    except ChainError as exc:
        # Model-or-fail: a hop that cannot run its model fails loud with a
        # clean error (job already marked failed) — never fabricated output.
        print(f"[error] chain {chain.id} job {job.job_id} failed loud: {exc}",
              file=sys.stderr)
        return 1
    print(f"chain={chain.id} job={job.job_id} final={res['final']}")
    for hop, info in res["hop_results"].items():
        print(f"  {hop}: {info['verdict']}")
    print("\nartifacts:")
    for a in ledger.get(job.job_id).artifacts:
        print(f"  {a['name']}  {a['sha256'][:12]}  {a['size']}B  by {a['produced_by']}")
    return 0 if res["final"] == "SHIPPED" else 2


def build_default_gate() -> EvidenceGate:
    """Generic gate: artifact requirements live in hop/workflow definitions,
    not here (research.md != review.md != solution.py)."""
    gate = EvidenceGate()
    gate.register_check("eval-json", eval_json_check())
    gate.register_check("exit-codes", exit_codes_check())
    return gate


def cmd_submit(args) -> int:
    ledger = _ledger(args)
    router = build_default_router()
    decision = router.classify(args.task)
    print(json.dumps(decision.to_dict(), indent=2))

    # EVERY prompt is a workflow: no direct-answer escape hatch. An unknown
    # task routes to `respond`, which still runs a job, writes RESPONSE.md,
    # and is verified (SHIP) before returning.
    job = ledger.submit(workflow_id=decision.workflow_id, input={"task": args.task})
    job.attach_route_decision(decision)
    ledger.update(job)

    # LEARN: every submit path records a route event (durable, per P2)
    learner = _learner(args)

    # dispatch through the shared registry: real workflows/chains per
    # workflow_id (research != review != build). Every id the router can
    # select has a real, model-gated workflow — there is NO collect node and
    # NO fabricated-output fallback for unregistered ids (fail loud instead).
    from nine.registry import CHAINS, WORKFLOWS

    if decision.workflow_id in CHAINS:
        from nine.chains.chain import ChainExecutor
        chain = CHAINS[decision.workflow_id]()
        cex = ChainExecutor(ledger, workdir=args.workdir, learner=learner)
        job_dir = Path(args.workdir) / job.job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "task.txt").write_text(args.task + "\n")
        if decision.workflow_id == "inbox-triage-task-report":
            (job_dir / "inbox.txt").write_text(args.task + "\n")
        res = cex.execute(chain, job, {"task": args.task}, decision=decision)
        print(f"chain={chain.id} job={job.job_id} final={res['final']}")
        return 0 if res["final"] == "SHIPPED" else 2

    if decision.workflow_id in WORKFLOWS:
        wf = WORKFLOWS[decision.workflow_id]()
    else:
        raise WorkflowError(
            f"unregistered workflow id '{decision.workflow_id}' — no collect "
            "fallback; nine is model-driven (router must only emit "
            "registered ids)"
        )

    from nine.registry import workflow_gate

    gate = workflow_gate(decision.workflow_id) or build_default_gate()
    executor = WorkflowExecutor(ledger, gate, workdir=args.workdir)
    try:
        result = executor.execute(wf, job, {"task": args.task})
    except WorkflowError as exc:
        # Model-or-fail: no offline fallback. Fail loud with a clean error
        # (job already marked failed in the ledger), never a canned answer.
        print(f"[error] job {job.job_id} failed loud: {exc}", file=sys.stderr)
        return 1

    # LEARN: one route event per completed workflow run
    _record_route_event(learner, job, decision, result["verdict"])

    print("\n[verdict]", result["verdict"]["verdict"], "-", result["verdict"]["summary"])
    if decision.workflow_id == "respond":
        resp_path = Path(args.workdir) / job.job_id / "RESPONSE.md"
        if resp_path.exists():
            print("[response]", resp_path.read_text(encoding="utf-8").strip().replace("\n", " "))
    print("[job]", job.job_id, "->", job.status)
    return 0


def _record_route_event(learner, job, decision, verdict: dict) -> None:
    """Append a route event for a one-shot workflow run (chain runs record
    per-hop events inside ChainExecutor). job is None for direct answers."""
    from nine.learn.learner import RouteEvent

    eval_results = verdict.get("eval_results") or {}
    learner.observe(
        RouteEvent(
            event_id=f"ev-{job.job_id[:8] if job else decision.task_redacted[:8]}",
            job_id=job.job_id if job else "",
            task_redacted=decision.task_redacted[:200],
            workflow_id=decision.workflow_id,
            confidence=float(decision.confidence),
            router_version=decision.router_version,
            verdict=verdict.get("verdict", "BLOCK"),
            checks_passed=sum(1 for r in eval_results.values() if r.get("passed")),
            checks_total=len(eval_results),
            fix_directive="",
        )
    )


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


def _learner(args):
    """Route-event store + learner on the CLI's durable event log."""
    from nine.learn.learner import Learner, RouteEventStore

    events_path = Path(args.events)
    return Learner(RouteEventStore(events_path))


def _print_candidate(c) -> None:
    print(f"{c.candidate_id}  [{c.kind:8s}] {c.status}")
    print(f"    {c.description}")
    if c.params:
        print(f"    params: {json.dumps(c.params)}")
    print()


def cmd_learn(args) -> int:
    """LEARN loop: events, candidates, and regression-gated apply/revert."""
    learner = _learner(args)
    action = args.action

    if action == "events":
        events = learner.store.all()
        print(f"{len(events)} route events")
        for ev in events[-20:]:
            print(f"  {ev.event_id}  {ev.workflow_id:28s} conf={ev.confidence:.2f} "
                  f"{ev.verdict:5s} {ev.recorded_at[:19]}")
        return 0

    if action == "candidates":
        cands = learner.cands.all()
        if not cands:
            print("no improvement candidates yet (run: nine learn scan)")
        for c in cands:
            _print_candidate(c)
        return 0

    if action == "scan":
        cands = learner.learn()
        print(f"scan produced {len(cands)} candidate(s)")
        for c in cands:
            _print_candidate(c)
        return 0

    if action == "apply":
        return _apply_candidate(learner, args.candidate_id)

    if action == "revert":
        return _revert_candidate(learner, args.candidate_id)

    print(f"unknown learn action: {action}")
    return 2


def _apply_candidate(learner, candidate_id: str) -> int:
    """Apply an approved candidate: regression-gated catalog change + commit.

    Doctrine: the LEARN loop never changes behavior silently. apply() runs
    the full hermetic suite BEFORE and AFTER the change, and only commits
    if both pass; any failure restores the catalog and aborts.
    """
    cand = learner.cands.get(candidate_id)
    if cand is None:
        print(f"no candidate {candidate_id}")
        return 2
    if cand.status != "pending":
        print(f"candidate {candidate_id} is {cand.status} (only pending can be applied)")
        return 2

    wf_id = cand.params.get("workflow_id", "")
    kw = cand.params.get("keyword", "")
    if cand.kind != "keyword" or not kw or not wf_id:
        print("this candidate is not auto-applicable (no actionable keyword); "
              "edit nine/router/catalog.json manually, then nine learn apply")
        return 2

    from nine.registry import load_catalog, save_catalog

    catalog = load_catalog()
    current = catalog.setdefault("keyword_overrides", {}).setdefault(wf_id, [])
    if kw in current:
        print(f"keyword '{kw}' already in catalog for {wf_id} — nothing to do")
        learner.cands.update_status(candidate_id, "applied")
        return 0

    # 1) pre-change gate: regression suite must be green before we touch anything
    if not _regression_green():
        print("regression suite FAILED before change — aborting apply")
        return 1

    # 2) apply the change
    current.append(kw)
    save_catalog(catalog)

    # 3) post-change gate: the new keyword must not break routing tests
    if not _regression_green():
        print("regression suite FAILED after change — restoring catalog")
        current.remove(kw)
        save_catalog(catalog)
        return 1

    # 4) durable, auditable commit
    _git_commit(f"learn apply {candidate_id}: add keyword '{kw}' to '{wf_id}'")
    learner.cands.update_status(candidate_id, "applied")
    print(f"applied {candidate_id}: keyword '{kw}' -> {wf_id}; "
          f"catalog committed (rollback: nine learn revert {candidate_id})")
    return 0


def _revert_candidate(learner, candidate_id: str) -> int:
    """Rollback an applied candidate: remove its keywords, re-gate, commit."""
    cand = learner.cands.get(candidate_id)
    if cand is None:
        print(f"no candidate {candidate_id}")
        return 2
    if cand.status != "applied":
        print(f"candidate {candidate_id} is {cand.status} (only applied can be reverted)")
        return 2

    from nine.registry import load_catalog, save_catalog

    wf_id = cand.params.get("workflow_id", "")
    kw = cand.params.get("keyword", "")
    catalog = load_catalog()
    overrides = catalog.get("keyword_overrides", {})
    bucket = overrides.get(wf_id, [])
    if kw not in bucket:
        print(f"keyword '{kw}' not present in catalog for {wf_id} — nothing to revert")
        learner.cands.update_status(candidate_id, "pending")
        return 0

    bucket.remove(kw)
    if not bucket:
        overrides.pop(wf_id, None)
    save_catalog(catalog)
    if not _regression_green():
        print("regression suite FAILED after revert — restoring catalog")
        bucket.append(kw)
        overrides[wf_id] = bucket
        save_catalog(catalog)
        return 1
    _git_commit(f"learn revert {candidate_id}: remove keyword '{kw}' from '{wf_id}'")
    learner.cands.update_status(candidate_id, "pending")
    print(f"reverted {candidate_id}: keyword '{kw}' removed from {wf_id}")
    return 0


def _regression_green() -> bool:
    """Hermetic regression suite (no API key, no network), run ISOLATED so
    the suite's own submits (which use the default jobs/events.jsonl) never
    pollute the operator's real event store."""
    import subprocess as _sp

    _py = sys.executable
    root = Path(__file__).resolve().parent.parent
    jobs = root / "jobs"
    evp, cand = jobs / "events.jsonl", jobs / "events.jsonl.candidates.jsonl"
    backup = []
    for p in (evp, cand):
        data = p.read_bytes() if p.exists() else None
        backup.append((p, data))
        if data is not None:
            p.unlink()
    try:
        env = dict(os.environ)
        env["GEMINI_API_KEY"] = ""
        r = _sp.run(
            [_py, "-m", "pytest", "tests/", "-q", "--tb=short"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        return r.returncode == 0
    finally:
        for p, data in backup:
            if data is None:
                if p.exists():
                    p.unlink()
            else:
                p.write_bytes(data)


def _git_commit(message: str) -> None:
    import subprocess as _sp

    root = Path(__file__).resolve().parent.parent
    _sp.run(["git", "-C", str(root), "add", "nine/router/catalog.json"], check=True)
    _sp.run(["git", "-C", str(root), "-c", "user.name=adamnorm4wd",
             "-c", "user.email=adamnorm4wd@atomicmail.io", "commit", "-m", message],
            check=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="nine", description="nine agent OS")
    p.add_argument("--ledger", default=DEFAULT_LEDGER, help="ledger path")
    p.add_argument("--events", default="jobs/events.jsonl", help="route-event store path")
    p.add_argument("--memory", default="jobs/memory.jsonl", help="semantic memory store path")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("submit")
    s.add_argument("task")
    s.add_argument("--ledger", default=DEFAULT_LEDGER)
    s.add_argument("--workdir", default="work")
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

    s = sub.add_parser("memory")
    s.add_argument("action", choices=["search", "list"])
    s.add_argument("query", nargs="?", default=None, help="search terms (search action)")
    s.set_defaults(fn=cmd_memory)

    s = sub.add_parser("learn")
    s.add_argument("action", choices=["events", "candidates", "scan", "apply", "revert"])
    s.add_argument("candidate_id", nargs="?", default=None,
                   help="candidate id for apply/revert")
    s.set_defaults(fn=cmd_learn)

    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
