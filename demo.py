#!/usr/bin/env python3
"""chow-lite live demo — the Taskmaster lane in one command.

Shows the full loop: inbox -> triage -> task -> report, with the job
ledger, route events, and the SHIP verdict with evidence. This is the
script used for the demo video and for judges to try instantly.

Run:  python demo.py "your task here"
"""
import sys
import tempfile
from pathlib import Path

from chowlite.chains.chain import ChainExecutor
from chowlite.chains.flagship import demo_lane
from chowlite.learn.learner import Learner, RouteEventStore
from chowlite.ledger.ledger import JSONLLedger


def main() -> int:
    task = sys.argv[1] if len(sys.argv) > 1 else "respond to customer refund question"
    with tempfile.TemporaryDirectory() as tmp:
        td = Path(tmp)
        ledger = JSONLLedger(td / "ledger.jsonl")
        events = RouteEventStore(td / "events.jsonl")
        learner = Learner(events)
        ex = ChainExecutor(ledger, workdir=td / "work", learner=learner)

        job = ledger.submit("inbox-triage-task-report", {"task": task})
        job_dir = td / "work" / job.job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        (job_dir / "inbox.txt").write_text(task + "\n")
        (job_dir / "task.txt").write_text(task + "\n")

        print("== chow-lite demo: inbox -> triage -> task -> report ==")
        print(f"task: {task}\n")
        res = ex.execute(demo_lane(), job, {"task": task})

        print(f"FINAL: {res['final']}")
        for hop, info in res["hop_results"].items():
            print(f"  {hop}: {info['verdict']}")
        print("\nroute events recorded:", len(events.all()))
        print("improvement candidates:", len(learner.learn()))
        print("\nartifacts:")
        for a in ledger.get(job.job_id).artifacts:
            print(f"  {a['name']}  {a['sha256'][:12]}  {a['size']}B  by {a['produced_by']}")
        return 0 if res["final"] == "SHIPPED" else 2


if __name__ == "__main__":
    sys.exit(main())
