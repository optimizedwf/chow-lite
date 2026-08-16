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

Exit codes: 0 ok, 1 error, 2 non-SHIP verdict (submit/chain). (An exit
code is NOT task success — check `nine status` for the SHIP/FIX/BLOCK
verdict.)
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from nine.ledger.ledger import InvalidTransition, JSONLLedger, LedgerError
from nine.router.classifier import Router
from nine.runtime.workflows import WorkflowError, WorkflowExecutor

DEFAULT_LEDGER = "jobs/ledger.jsonl"


def _ledger(args) -> JSONLLedger:
    return JSONLLedger(getattr(args, "ledger", DEFAULT_LEDGER))


def _validate_node_timeout_env() -> None:
    """Fail FAST on NINE_NODE_TIMEOUT_S=0/-N — BEFORE the job is durable.

    Node.__post_init__ raises ValueError for timeout < 1, but that fires
    inside _execute_job / server submit AFTER ledger.submit already wrote
    the job (torture-22 finding 2): cmd_submit caught the ValueError and
    left a permanent 'submitted' zombie (recover only re-runs
    blocked/failed), cmd_chain raw-tracebacked, and the server 500'd with
    a zombie. Every entry point validates the env before any state change.
    """
    raw = os.environ.get("NINE_NODE_TIMEOUT_S")
    if not raw:
        return
    try:
        val = int(raw)
    except ValueError:
        return  # malformed -> keep node default (Node.__post_init__ parity)
    if val < 1:
        raise ValueError(
            f"NINE_NODE_TIMEOUT_S must be >= 1 or unset "
            f"(got {val!r}); 0 does NOT mean 'no timeout'"
        )


def _routing_model():
    """Model-first routing when the active backend has a key; else None.

    The CLI router MUST use the model when available (bench finding): the
    keyword substrate alone misroutes on substrings ("implement" inside
    "implementation"), and eval metadata would lie about which lane served
    the job. Any model error degrades to the deterministic keyword
    substrate inside Router.classify — routing never crashes the loop.
    Backend: Gemini direct by default; NINE_LLM_BACKEND=openai routes via
    the testing tunnel (DS4 Flash).
    """
    from nine.runtime import llm_provider

    return llm_provider.make_model_client()


def build_default_router() -> Router:
    from nine.registry import HOP_DESCRIPTIONS, KEYWORDS
    r = Router(model=_routing_model())
    for wf_id, kws in KEYWORDS.items():
        r.register(wf_id, kws, HOP_DESCRIPTIONS.get(wf_id, ""))
    return r


def cmd_memory(args) -> int:
    """Semantic memory: search distilled hop summaries / list recent entries."""
    from nine.memory.graph import get_memory_graph

    try:
        # torture-14 F7: a bad --memory path raw-tracebacked FileExistsError
        # from LocalMemoryGraph.__init__ mkdir (T12-F8 only wrapped the
        # ledger). One clean line.
        mem = get_memory_graph(path=getattr(args, "memory", "jobs/memory.jsonl"))
    except OSError as e:
        print(f"error: cannot open memory store: {e}", file=sys.stderr)
        return 1
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
            # torture-14 F9: a valid-JSON wrong-shape record (hand-edited or
            # version-skewed store) must not KeyError-crash search — skip it.
            if not (isinstance(h, dict)
                    and all(k in h for k in ("memory_id", "hop_id",
                                             "artifact_name", "verdict",
                                             "created_at", "summary"))):
                continue
            print(f"{h['memory_id']}  [{h['hop_id']}] {h['artifact_name']}  "
                  f"verdict={h['verdict']}  {h['created_at'][:19]}")
            print(f"    {h['summary'][:160].replace(chr(10), ' ')}")
        return 0
    # list
    rows = []
    local_path = getattr(mem, "path", None)
    if isinstance(local_path, Path) and local_path.exists():
        import json as _json

        # torture-6 F8: one corrupt (or non-UTF8) line must NOT raw-traceback
        # `nine memory list` — skip it like the search path does.
        try:
            text = open(local_path, encoding="utf-8", errors="replace").read()
        except OSError as e:
            print(f"error: cannot read memory store {local_path}: {e}",
                  file=sys.stderr)
            return 1
        for line in reversed(text.splitlines()):
            if not line.strip():
                continue
            try:
                rows.append(_json.loads(line))
            except (ValueError, TypeError):
                continue  # corrupt line skipped
    else:
        rows = list(mem.search_context("latest", k=10))
    if not rows:
        print("memory is empty (run a chain to record hop summaries)")
        return 0
    for h in rows[-10:][::-1]:
        # torture-14 F9: same shape guard as search — a wrong-shape record
        # must not KeyError-crash `nine memory list`.
        if not (isinstance(h, dict)
                and all(k in h for k in ("memory_id", "chain_id", "hop_id",
                                         "artifact_name", "verdict"))):
            continue
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

    try:
        ledger = _ledger(args)
    except LedgerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        # torture-22 finding 2: chain hops build Nodes with
        # NINE_NODE_TIMEOUT_S — a 0/-N value ValueError'd AFTER the job
        # dir setup (raw traceback, no ledger row). Fail with one clean
        # line BEFORE any state change.
        _validate_node_timeout_env()
        chain = chains[args.chain_id]()
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        # torture-14 F7: learn/memory store construction on a bad --events/
        # --memory path raw-tracebacked FileExistsError (T12-F8 only wrapped
        # the ledger). One clean line for the learn + memory stores too.
        ex = ChainExecutor(ledger, workdir=getattr(args, "workdir", "work"),
                           learner=_learner(args),
                           memory=get_memory_graph(path=args.memory))
    except OSError as e:
        print(f"error: cannot open learn/memory store: {e}", file=sys.stderr)
        return 1

    # seed the chain job dir with the task input file
    try:
        job = ledger.submit(chain.id, input={"task": args.task})
    except LedgerError as e:
        print(f"error: cannot submit to ledger: {e}", file=sys.stderr)
        return 1
    job_dir = Path(getattr(args, "workdir", "work")) / job.job_id
    try:
        # torture-16 F7: a `work` FILE in cwd made mkdir raw-traceback
        # FileExistsError on submit/recover/chain/server — one clean line.
        job_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"error: cannot create job dir {job_dir}: {e}",
              file=sys.stderr)
        return 1
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


def _execute_job(ledger, job, task: str, args) -> int:
    """Execute a job through the shared registry (chain or workflow).

    Shared by `nine submit` and `nine recover`: both dispatch on the job's
    workflow_id, write task.txt into the job dir, run the registry
    workflow/chain, and return an exit code (0 SHIP / 1 error / 2 non-SHIP).
    """
    # LEARN: every completed run records a route event (durable, per P2).
    # torture-16 F7: a bad --events path (parent component is a FILE)
    # raw-tracebacked FileExistsError from RouteEventStore's mkdir — the
    # T14-F7 guard covered cmd_learn/cmd_chain but not this shared path.
    try:
        learner = _learner(args)
    except OSError as e:
        print(f"error: cannot open event store: {e}", file=sys.stderr)
        return 1
    decision = getattr(job, "route_decision", None)
    if isinstance(decision, dict) and decision.get("workflow_id"):
        # ledger stores route_decision via to_dict(); restore the object so
        # consumers (route events, ChainExecutor) see real attributes
        from nine.router.classifier import RouteDecision

        try:
            decision = RouteDecision(**decision)
        except TypeError:
            decision = None

    # dispatch through the shared registry: real workflows/chains per
    # workflow_id (research != review != build). Every id the router can
    # select has a real, model-gated workflow — there is NO collect node and
    # NO fabricated-output fallback for unregistered ids (fail loud instead).
    from nine.registry import CHAINS, WORKFLOWS, resolve_gate

    job_dir = Path(getattr(args, "workdir", "work")) / job.job_id
    try:
        # torture-16 F7: a `work` FILE in cwd made mkdir raw-traceback
        # FileExistsError on submit/recover/chain/server — one clean line.
        job_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        print(f"error: cannot create job dir {job_dir}: {e}",
              file=sys.stderr)
        return 1
    (job_dir / "task.txt").write_text(task + "\n")
    if job.workflow_id == "inbox-triage-task-report":
        (job_dir / "inbox.txt").write_text(task + "\n")

    if job.workflow_id in CHAINS:
        from nine.chains.chain import ChainError, ChainExecutor
        chain = CHAINS[job.workflow_id]()
        cex = ChainExecutor(ledger, workdir=getattr(args, "workdir", "work"), learner=learner)
        try:
            res = cex.execute(chain, job, {"task": task}, decision=decision)
        except ChainError as exc:
            # torture-7 F5: recover of a chain job raw-tracebacked when a
            # hop failed loud (cmd_chain had the clean catch, this path did
            # not). Fail loud with the same ONE clean line.
            print(f"[error] job {job.job_id} failed loud: {exc}",
                  file=sys.stderr)
            # T20-F3 (slice 37): a failed-loud run IS a route observation —
            # the LEARN loop must see failures, not just SHIPs. Record a
            # FAILED verdict event before returning (README: every submit
            # path appends a route event and the verdict).
            _record_route_event(
                learner, job, decision,
                {"verdict": "FAILED", "eval_results": {}},
            )
            return 1
        print(f"chain={chain.id} job={job.job_id} final={res['final']}")
        return 0 if res["final"] == "SHIPPED" else 2

    if job.workflow_id in WORKFLOWS:
        wf = WORKFLOWS[job.workflow_id]()
    else:
        raise WorkflowError(
            f"unregistered workflow id '{job.workflow_id}' — no collect "
            "fallback; nine is model-driven (router must only emit "
            "registered ids)"
        )

    gate = resolve_gate(job.workflow_id)
    executor = WorkflowExecutor(ledger, gate, workdir=getattr(args, "workdir", "work"))
    try:
        result = executor.execute(wf, job, {"task": task})
    except WorkflowError as exc:
        # Model-or-fail: no offline fallback. Fail loud with a clean error
        # (job already marked failed in the ledger), never a canned answer.
        print(f"[error] job {job.job_id} failed loud: {exc}", file=sys.stderr)
        # T20-F3 (slice 37): failed-loud runs record a FAILED route event
        # so the learn loop sees the failure (README contract: every submit
        # path appends a route event and the verdict).
        _record_route_event(
            learner, job, decision,
            {"verdict": "FAILED", "eval_results": {}},
        )
        return 1
    except OSError as exc:
        # torture-21 F3 (torture-22 finding 3; server parity, torture-18
        # F5): the executor's manifest registration reads artifacts with
        # read_bytes() — an unreadable artifact (chmod 000) raw-tracebacked
        # PermissionError and left the job stuck 'running' (a zombie:
        # recover --force only salvages blocked/failed). One clean line +
        # a durable failed mark, matching the server's OSError -> clean-502.
        print(f"[error] job {job.job_id} failed loud: {exc}", file=sys.stderr)
        try:
            job.transition("failed")
            ledger.update(job)
        except (InvalidTransition, LedgerError, OSError):
            pass  # best-effort durable mark; the clean line is the contract
        _record_route_event(
            learner, job, decision,
            {"verdict": "FAILED", "eval_results": {}},
        )
        return 1

    # LEARN: one route event per completed workflow run
    _record_route_event(learner, job, decision, result["verdict"])

    print("\n[verdict]", result["verdict"]["verdict"], "-", result["verdict"]["summary"])
    if job.workflow_id == "respond":
        resp_path = job_dir / "RESPONSE.md"
        if resp_path.exists():
            print("[response]", resp_path.read_text(encoding="utf-8").strip().replace("\n", " "))
    print("[job]", job.job_id, "->", job.status)
    # a non-SHIP verdict is NOT success for automation: mirror the chain
    # path (exit 2) so CI/scripts cannot treat blocked/failed jobs as ok.
    if result["verdict"]["verdict"] != "SHIP":
        print(
            f"[warn] job {job.job_id} not SHIPPED "
            f"(verdict {result['verdict']['verdict']})",
            file=sys.stderr,
        )
        return 2
    return 0


def cmd_submit(args) -> int:
    try:
        ledger = _ledger(args)
    except LedgerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    router = build_default_router()
    decision = router.classify(args.task)
    print(json.dumps(decision.to_dict(), indent=2))

    # EVERY prompt is a workflow: no direct-answer escape hatch. An unknown
    # task routes to `respond`, which still runs a job, writes RESPONSE.md,
    # and is verified (SHIP) before returning.
    from nine.registry import CHAINS, WORKFLOWS

    if decision.workflow_id not in WORKFLOWS and decision.workflow_id not in CHAINS:
        # torture-12 F6: a learned/catalog keyword can point at a workflow
        # id that is no longer executable (plugin removed/renamed) — submit
        # would raw-traceback a WorkflowError. Refuse BEFORE submitting.
        print(f"error: routed workflow id '{decision.workflow_id}' is not "
              "registered (removed plugin or stale learned keyword?) — not "
              "submitting.", file=sys.stderr)
        return 1
    try:
        # torture-22 finding 2: NINE_NODE_TIMEOUT_S=0/-N must fail BEFORE
        # ledger.submit — the Node ValueError fires inside _execute_job
        # (after the job is durable) and left a permanent 'submitted'
        # zombie (recover only re-runs blocked/failed).
        _validate_node_timeout_env()
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        job = ledger.submit(workflow_id=decision.workflow_id, input={"task": args.task})
        job.attach_route_decision(decision)
        ledger.update(job)
    except LedgerError as e:
        # torture-25 F1 (MED): the PRIMARY submit path sat outside every
        # try — an un-appendable ledger (chmod 444, full disk) raw-tracebacked
        # LedgerError. Every other command promises ONE clean error: line;
        # submit must too (job never became durable, safe to refuse).
        print(f"error: cannot submit to ledger: {e}", file=sys.stderr)
        return 1
    try:
        return _execute_job(ledger, job, args.task, args)
    except (WorkflowError, ValueError) as exc:
        # torture-12 F6 (belt): a route that slips past the check still
        # fails with ONE clean line, never a raw traceback. ValueError
        # included (torture-16 F1 belt): a CANCELLED verdict racing a
        # schema-validated route event must not raw-traceback submit.
        print(f"[error] job {job.job_id} failed loud: {exc}", file=sys.stderr)
        return 1


def _record_route_event(learner, job, decision, verdict: dict) -> None:
    """Append a route event for a one-shot workflow run (chain runs record
    per-hop events inside ChainExecutor). job is None for direct answers."""
    if decision is None:
        return  # nothing real to learn from (stubbed/restored decision)
    if verdict.get("verdict") == "CANCELLED":
        # torture-16 F1: an operator-cancelled run never completed — there
        # is nothing to learn, and CANCELLED is not a route-event verdict
        # (the schema would reject it, raw-tracebacking submit/recover and
        # losing the event). Skip the observation entirely.
        return
    from nine.learn.learner import RouteEvent

    eval_results = verdict.get("eval_results") or {}
    try:
        learner.observe(
            RouteEvent(
                event_id=f"ev-{job.job_id[:8] if job else decision.task_redacted[:8]}"
                         f"-{int((job.metadata or {}).get('run_seq', 0)) if job else 0}",
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
    except OSError as exc:
        # torture-21 F1 (torture-22 finding 1): LEARN is a best-effort
        # side effect AFTER the verdict is durable — a broken events store
        # must not turn a shipped/blocked job into a raw traceback (CLI)
        # or HTTP 500 (server), and a client retry must not duplicate the
        # already-committed run.
        print(f"WARNING: route-event write skipped ({exc}); "
              "job verdict already durable", file=sys.stderr)


def cmd_status(args) -> int:
    try:
        job = _ledger(args).get(args.job_id)
    except LedgerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(job.to_dict(), indent=2))
    return 0


def cmd_discover(args) -> int:
    # torture-12 F8: an unusable --ledger path must be ONE clean line, not
    # a raw traceback (same contract as cmd_status/artifacts/cancel).
    # T20-F6 (slice 37): an unknown --status used to silently print
    # "0 job(s)" and exit 0 — the operator could not tell a typo from an
    # empty ledger. Validate the enum up front.
    if args.status is not None:
        from nine.ledger.ledger import VALID_STATUSES

        if args.status not in VALID_STATUSES:
            valid = ", ".join(sorted(VALID_STATUSES))
            print(
                f"error: unknown status {args.status!r} "
                f"(valid: {valid})",
                file=sys.stderr,
            )
            return 1
    try:
        jobs = _ledger(args).discover(status=args.status)
    except LedgerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
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
    try:
        job = _ledger(args).cancel(args.job_id)
    except LedgerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"cancelled {job.job_id} -> {job.status}")
    return 0


def cmd_recover(args) -> int:
    """Re-execute a blocked/failed job: fresh evidence, same task + workflow.

    recover is not a tombstone: it clears the stale attempt artifacts from
    the job dir, transitions blocked/failed -> recovered, then re-runs the
    SAME workflow/chain through the registry (torture finding T1-F7:
    recover used to park jobs in a dead-end status forever).
    """
    try:
        ledger = _ledger(args)
    except LedgerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # The RAW task survives in task.txt (ledger input is redacted for
    # display) — check it BEFORE any state change. torture-5 F4: if task.txt
    # is missing, re-executing from the redacted ledger input would SHIP
    # corrupted output as a verified job. Refuse loudly — the true task is
    # unrecoverable after a workdir hiccup, and the job stays blocked/failed
    # so the operator can restore the workdir and try again.
    try:
        # torture-13 F1: ledger.get raises LedgerError on an unknown id —
        # outside any try it raw-tracebacked (the slice-32 F8 clean-error
        # claim skipped recover's job-get path). One clean line instead.
        job = ledger.get(args.job_id)
    except LedgerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    # torture-12 F5: recover must not tombstone jobs it cannot re-execute.
    # A chain HOP job's workflow_id is "<chain-id>::<hop-id>" — not a
    # registered workflow/chain — so _execute_job below would raw-traceback
    # a WorkflowError AFTER the job dir was wiped and the job transitioned
    # to recovered (a dead-end tombstone). Validate BEFORE any state change.
    from nine.registry import CHAINS, WORKFLOWS

    if job.workflow_id not in WORKFLOWS and job.workflow_id not in CHAINS:
        hint = ("chain hop jobs are re-run by recovering the owning chain "
                "job" if "::" in job.workflow_id else
                "the workflow is not registered (removed plugin?)")
        print(f"error: cannot recover {args.job_id}: workflow id "
              f"'{job.workflow_id}' is not registered - {hint}.",
              file=sys.stderr)
        return 1
    try:
        # torture-22 finding 2: recover re-runs _execute_job, where the
        # Node timeout ValueError would fire AFTER the 'recovered'
        # transition + job-dir wipe (zombie at 'recovered'). Fail before
        # any state change.
        _validate_node_timeout_env()
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    job_dir = Path(getattr(args, "workdir", "work")) / job.job_id
    # torture-8 F2: a job_dir that IS a symlink means the workspace was
    # already compromised (a model-driven bash node can replace it with a
    # link to an arbitrary directory). recover's wipe would then DELETE
    # through the link — refuse loudly before any read/write/delete.
    if job_dir.is_symlink():
        print(f"error: cannot recover {job.job_id}: the job directory "
              f"{job_dir} is a symlink (workspace compromised or moved). "
              "Refusing to wipe through it. Restore a real directory or "
              "delete the symlink and re-submit.", file=sys.stderr)
        return 1
    task = ""
    task_txt = job_dir / "task.txt"
    # is_file() (not exists()): a FIFO at task.txt would make read_text
    # block (torture-24 F1 family); a corrupt task.txt must refuse cleanly.
    if task_txt.is_file():
        try:
            task = task_txt.read_text(encoding="utf-8").rstrip("\n")
        except (UnicodeDecodeError, OSError):
            # torture-23 F3 (LOW): a non-UTF-8 task.txt raw-tracebacked.
            # torture-25 F2 (LOW): a chmod-000 task.txt passes is_file()
            # and read_text raises PermissionError (an OSError) — the same
            # clean refusal. Re-executing from a corrupted raw task could
            # SHIP garbage as a verified job.
            print(f"error: cannot recover {job.job_id}: task.txt is not "
                  "readable (corrupt, unreadable, or not valid UTF-8). "
                  "Restore the workdir or re-submit the task.", file=sys.stderr)
            return 1
    if not task:
        print(f"error: cannot recover {job.job_id}: task.txt is missing (raw "
              "task not available; the ledger only stores the redacted "
              "task). Restore the workdir or re-submit the task.", file=sys.stderr)
        return 1

    force = bool(getattr(args, "force", False))
    if force:
        try:
            live = ledger.refresh(args.job_id)
        except LedgerError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        if live.status == "running":
            # torture-8 F6: a job left 'running' by a crash (SIGKILL, power
            # loss, deploy) was UNRECOVERABLE - recover refused (blocked/
            # failed only) and cancel tombstoned it at 'cancelled' with no
            # way forward. --force degrades a stale running job to failed
            # (legal transition) so the normal recover path can re-run it.
            print(f"warning: {args.job_id} is 'running' (stale after a "
                  "crash?) - --force degrades it to failed and re-executes",
                  file=sys.stderr)
            live.transition("failed")
            ledger.update(live)
            # torture-10 F1: update() only appends the durable line — the
            # in-memory cache still says 'running', so the recover() below
            # (which reads the CACHE) would error "is running, only
            # blocked/failed can be recovered" and force a SECOND invocation.
            # Sync the cache to the durable state we just wrote: one call.
            ledger._jobs[args.job_id] = live
    # torture-23 F1 (HIGH): the artifact wipe used to run AFTER
    # ledger.recover() stamped a durable 'recovered' line — a
    # PermissionError in unlink/rmtree raw-tracebacked and left the job
    # durably 'recovered' with stale artifacts (a tombstone a second
    # recover refuses: 'recovered' is not in the recoverable set). Wipe
    # FIRST, while the job is still blocked/failed (the --force path above
    # already degraded a stale running job to failed), and surface a clean
    # error on OSError — the job stays recoverable, the operator fixes
    # permissions and retries.
    if job.status in ("blocked", "failed") and job_dir.exists():
        try:
            for p in job_dir.iterdir():
                if p.is_file() or p.is_symlink():
                    p.unlink()
                elif p.is_dir():
                    shutil.rmtree(p)
        except OSError as e:
            print(f"error: cannot recover {job.job_id}: failed to clear "
                  f"stale artifacts in {job_dir}: {e}", file=sys.stderr)
            return 1
    try:
        job = ledger.recover(args.job_id)
    except LedgerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"recovering {job.job_id} ({job.workflow_id}) — re-executing")
    try:
        return _execute_job(ledger, job, task, args)
    except (WorkflowError, ValueError) as exc:
        # torture-12 F5 (belt): even after the id check a hop could raise
        # WorkflowError mid-flight — one clean line, like the ChainError
        # path (torture-7 F5), never a raw traceback. ValueError included
        # (torture-16 F1 belt): a cancelled recover must not raw-traceback.
        print(f"[error] job {job.job_id} failed loud: {exc}", file=sys.stderr)
        return 1


def cmd_stats(args) -> int:
    try:
        stats = _ledger(args).stats()
    except LedgerError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(json.dumps(stats, indent=2))
    return 0


def _learner(args):
    """Route-event store + learner on the CLI's durable event log."""
    from nine.learn.learner import Learner, RouteEventStore

    events_path = Path(getattr(args, "events", "jobs/events.jsonl"))
    return Learner(RouteEventStore(events_path))


def _print_candidate(c) -> None:
    print(f"{c.candidate_id}  [{c.kind:8s}] {c.status}")
    print(f"    {c.description}")
    if c.params:
        print(f"    params: {json.dumps(c.params)}")
    print()


def cmd_learn(args) -> int:
    """LEARN loop: events, candidates, and regression-gated apply/revert."""
    try:
        # torture-14 F7: RouteEventStore.__init__ mkdir on a bad --events
        # path raw-tracebacked FileExistsError. One clean line.
        learner = _learner(args)
    except OSError as e:
        print(f"error: cannot open event store: {e}", file=sys.stderr)
        return 1
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

    if action in ("apply", "revert"):
        if not args.candidate_id:
            # T20-F6 (slice 37): `nine learn apply` with no id printed
            # "no candidate None" — a confused operator sees a fake
            # candidate id instead of the usage contract.
            print(
                f"error: 'nine learn {action}' requires a candidate_id "
                "(see `nine learn candidates`)",
                file=sys.stderr,
            )
            return 2
        if action == "apply":
            return _apply_candidate(learner, args.candidate_id)
        return _revert_candidate(learner, args.candidate_id)

    print(f"unknown learn action: {action}")
    return 2


def _catalog_is_committed() -> bool | None:
    """True: catalog.json matches git HEAD. False: uncommitted catalog
    changes exist. None: cannot verify (not a git repo / git missing).

    torture-18 F4: the "already present" branches flipped candidate status
    WITHOUT checking whether the on-disk catalog mutation (left behind by a
    failed T16-F9 commit) was ever committed — one retry later the durable
    audit commit silently never lands while the status claims applied/
    pending. Only a COMMITTED catalog state may flip a candidate.
    """
    import subprocess as _sp

    _root = Path(__file__).resolve().parent.parent
    try:
        out = _sp.run(
            ["git", "-C", str(_root), "status", "--porcelain",
             "--", "nine/router/catalog.json"],
            check=True, capture_output=True, text=True)
        return out.stdout.strip() == ""
    except (OSError, _sp.CalledProcessError):
        return None


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

    from nine.registry import NON_ROUTABLE_IDS, load_catalog, save_catalog

    # torture-15 F8: the refusal must be case/whitespace-insensitive — a
    # candidate whose workflow_id is " Inbox-Triage-Task-Report " (or any
    # case variant) must not slip past the exact-match guard and re-expose
    # the canned demo lane to production routing.
    if wf_id.strip().casefold() in {i.strip().casefold()
                                    for i in NON_ROUTABLE_IDS}:
        # torture-14 F1: the LEARN loop must never re-expose the canned demo
        # lane to production routing — T5-F2's keyword ban is enforceable at
        # the merge, but apply() is the ONE file path that could re-add it.
        print(f"error: cannot apply a keyword for non-routable workflow id "
              f"'{wf_id}' (demo lane is never reachable from the router)",
              file=sys.stderr)
        return 1

    catalog = load_catalog()
    current = catalog.setdefault("keyword_overrides", {}).setdefault(wf_id, [])
    if not isinstance(current, list):
        # torture-14 F8: a valid-JSON wrong-shape catalog entry (string
        # instead of list) raw-tracebacked 'str' object has no attribute
        # 'append'. Refuse with the established shape-guard warning; never
        # mutate a corrupt bucket.
        print(f"error: catalog keyword_overrides[{wf_id!r}] is not a list; "
              "fix nine/router/catalog.json before applying", file=sys.stderr)
        return 1
    if kw in current:
        # torture-18 F4: "already in catalog" must only mark applied when
        # the on-disk state is actually COMMITTED. A retry after a failed
        # commit (T16-F9) finds the keyword already there and used to flip
        # status to applied with NO commit and NO regression run.
        committed = _catalog_is_committed()
        if committed is False or committed is None:
            print(
                "error: keyword already in catalog but catalog.json has "
                "uncommitted changes (or no git) — commit the catalog "
                "change manually; candidate was NOT marked applied",
                file=sys.stderr)
            return 1
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

    # 4) durable, auditable commit (torture-16 F9: on failure the catalog
    # change stays on disk, the candidate is NOT marked applied, and the
    # operator gets one loud line — never a raw traceback mid-mutation).
    if not _git_commit(f"learn apply {candidate_id}: add keyword '{kw}' to '{wf_id}'"):
        return 1
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
    if not isinstance(bucket, list):
        # torture-14 F8: same shape guard as apply — a string bucket must
        # not crash revert with AttributeError.
        print(f"error: catalog keyword_overrides[{wf_id!r}] is not a list; "
              "fix nine/router/catalog.json before reverting", file=sys.stderr)
        return 1
    if kw not in bucket:
        # torture-18 F4: symmetric — only flip to pending when the absent
        # keyword is actually committed on disk (a retry after a failed
        # revert commit used to mark pending while the rollback commit
        # never landed).
        committed = _catalog_is_committed()
        if committed is False or committed is None:
            print(
                "error: keyword already absent but catalog.json has "
                "uncommitted changes (or no git) — commit the catalog "
                "change manually; candidate was NOT marked pending",
                file=sys.stderr)
            return 1
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
    if not _git_commit(f"learn revert {candidate_id}: remove keyword '{kw}' from '{wf_id}'"):
        # torture-16 F9: same contract as apply — candidate status untouched.
        return 1
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
        env["NINE_LLM_BACKEND"] = "gemini"  # hermetic self-test: never the tunnel
        env["OPENCODE_GO_API_KEY"] = ""
        env["NINE_LLM_API_KEY"] = ""
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


def _git_commit(message: str) -> bool:
    """Commit catalog.json; False (with a loud warning) when impossible.

    torture-16 F9: on a non-git deployment (pip/sdist install, tarball,
    Cloud Run image) `git` raises CalledProcessError; with check=True
    uncaught, `nine learn apply`/`revert` raw-tracebacked AFTER the catalog
    was already mutated and BEFORE the candidate status flipped — silent
    partial mutation. Now the commit failure is LOUD and the caller leaves
    the candidate's status untouched (never "applied" on a failed commit).
    """
    import subprocess as _sp

    root = Path(__file__).resolve().parent.parent
    try:
        _sp.run(["git", "-C", str(root), "add", "nine/router/catalog.json"],
                check=True, capture_output=True, text=True)
        _sp.run(["git", "-C", str(root), "-c", "user.name=adamnorm4wd",
                 "-c", "user.email=adamnorm4wd@atomicmail.io",
                 "commit", "-m", message],
                check=True, capture_output=True, text=True)
        return True
    except (OSError, _sp.CalledProcessError) as exc:
        print(
            "warning: catalog.json changed on disk but the commit FAILED "
            f"({type(exc).__name__}: {exc}) — not a git repo or git "
            "unavailable. Commit the catalog change manually; the candidate "
            "was NOT marked applied.", file=sys.stderr,
        )
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="nine", description="nine agent OS")
    p.add_argument("--ledger", default=DEFAULT_LEDGER, help="ledger path")
    p.add_argument("--events", default="jobs/events.jsonl", help="route-event store path")
    p.add_argument("--memory", default="jobs/memory.jsonl", help="semantic memory store path")
    # torture-6 F7: --workdir belongs on the parent parser too, otherwise
    # `nine --workdir /tmp/x submit ...` dies with a misleading error while
    # `submit --workdir` works (surface asymmetry left by T4-F6). SUPPRESS so
    # subparsers that re-declare it cannot clobber a pre-subcommand value.
    p.add_argument("--workdir", default=argparse.SUPPRESS, help="job workdir")
    sub = p.add_subparsers(dest="cmd", required=True)

    # T4-F6: submit/chain re-declare --ledger/--workdir. Without
    # default=argparse.SUPPRESS the subparser default CLOBBERS a global
    # value given BEFORE the subcommand (`nine --ledger /tmp/x submit ...`
    # silently wrote to the production ledger). SUPPRESS keeps the global
    # value when the flag appears before the subcommand while still
    # accepting it after.
    s = sub.add_parser("submit")
    s.add_argument("task")
    s.add_argument("--ledger", default=argparse.SUPPRESS)
    s.add_argument("--workdir", default=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_submit)

    s = sub.add_parser("chain")
    s.add_argument("chain_id")
    s.add_argument("task")
    s.add_argument("--ledger", default=argparse.SUPPRESS)
    s.add_argument("--workdir", default=argparse.SUPPRESS)
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
    s.add_argument("--force", action="store_true",
                   help="recover a job stuck at 'running' by a crash "
                        "(degrades it to failed first; loud warning)")
    s.add_argument("--workdir", default=argparse.SUPPRESS)
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
